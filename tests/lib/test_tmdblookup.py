"""Tests for medialib.lib.tmdblookup - TMDb-backed IMDb id lookup, and
Plex/Jellyfin id tagging.

What is pinned here: the exact certainty rule of the id lookup over the jq
null/empty edge cases (a missing primary title is the
literal word "null", an empty-string title is dropped, an empty alternative is
skipped while a null one is not), the curl argv the lookup hands the network, the
safety skips a rename records, and the no-key warning.

``normalize_title`` is the awkward one, because what it delegates to is not the
same everywhere. A glibc iconv is its source of truth and the recorded folds
are that iconv's answers - so the two cases about the codepoints that make it
GIVE UP ask this host before asserting on what happens then, because which
codepoints those are belongs to the installed glibc. A host whose iconv is GNU
libiconv (macOS, MSYS) spells accents out instead and is not used at all; the
cases for that rung, and for the Python fold it falls to, are further down, and
one of them holds the two to the same answers.
"""

import json
import re
import subprocess

import pytest

from medialib.lib import tmdblookup
from medialib.lib.safety import SkipLog

pytestmark = pytest.mark.stubbed

_BASE = tmdblookup._BASE


def _iconv_gives_up_on(text: str) -> bool:
    """Whether this host's iconv FAILS on ``text`` rather than transliterating
    it: the condition the two reset cases below are about.

    A handful of codepoints transliterate AND fail, which resets the string to
    the original, and which ones do is glibc's business and its version's - so
    the two cases ask this host rather than recording one host's answer.
    """
    try:
        done = subprocess.run(
            ["iconv", "-f", "UTF-8", "-t", "ASCII//TRANSLIT"],
            input=text.encode("utf-8"), capture_output=True)
    except OSError:
        return False
    return done.returncode != 0


# Both reset cases are about what the module does with ICONV's answer, so they
# are only asked on a host the module actually reaches iconv on - a GNU
# libiconv one (macOS, MSYS) folds in Python and never sees a give-up at all.
_RESETS_MICRO = tmdblookup._iconv_drops_accents() and _iconv_gives_up_on("a\u00b5b")
_RESETS_FRACTION = (tmdblookup._iconv_drops_accents()
                    and _iconv_gives_up_on("\u00bd life"))
tmdblookup.reset_iconv_flavour()


def _stub_iconv(monkeypatch, returncode: int, stdout: bytes = b""):
    """Stand a fake iconv in front of the fold, answering the module's flavour
    probe the way a glibc one does and every other call as the case asks.

    The probe needs answering separately: a fake that failed it too would send
    the fold down the Python path, and these cases are about what the module
    does with what ICONV said.
    """
    probe = tmdblookup._TRANSLIT_PROBE.encode("utf-8")
    answer = tmdblookup._TRANSLIT_PROBE_GLIBC.encode("utf-8")

    def fake_run(*_args, input=None, **_kwargs):
        if input == probe:
            return subprocess.CompletedProcess([], 0, answer, b"")
        return subprocess.CompletedProcess([], returncode, stdout, b"")

    tmdblookup.reset_iconv_flavour()
    monkeypatch.setattr(tmdblookup.subprocess, "run", fake_run)


class TestNormalizeTitle:
    @pytest.mark.parametrize("title,want", [
        ("Amélie", "amelie"),
        ("SPIDER-MAN: Homecoming", "spider man homecoming"),
        ("Café au lait", "cafe au lait"),
        ("Übung 2: The Æra", "ubung 2 the aera"),
        ("Bàtman", "batman"),
        ("(Weird)  Title!!", "weird title"),
        ("Mì Đội", "mi doi"),
        ("  spaced   out  ", "spaced out"),
        ("half life 3", "half life 3"),
    ])
    def test_folds_accents_case_and_punctuation(self, title, want):
        assert tmdblookup.normalize_title(title) == want

    def test_non_alphanumeric_runs_collapse_to_one_space(self):
        assert tmdblookup.normalize_title("a---b..c   d") == "a b c d"

    @pytest.mark.skipif(not _RESETS_MICRO,
                        reason="this host's iconv transliterates U+00B5")
    def test_an_untransliteratable_codepoint_resets_to_the_original(self):
        # micro U+00B5 makes this host's iconv fail rather than emit "?", so the
        # shell's `|| s="$1"` stands the original up and only the fold applies:
        # the micro becomes a space, the letters stay
        assert tmdblookup.normalize_title("aµb") == "a b"

    @pytest.mark.skipif(not _RESETS_FRACTION,
                        reason="this host's iconv transliterates U+00BD")
    def test_a_fraction_resets_to_the_original(self):
        # U+00BD (one half) also falls in the reset set, so "½ life" keeps its
        # letters and loses the fraction to a dropped run
        assert tmdblookup.normalize_title("½ life") == "life"

    def test_the_reset_itself_needs_no_iconv_to_pin(self, monkeypatch):
        """WHICH codepoints make iconv give up is the host's; that giving up
        resets the string to the original is this module's, and is pinned
        without asking any host."""
        _stub_iconv(monkeypatch, 1)
        assert tmdblookup.normalize_title("Amélie 2") == "am lie 2"

    def test_and_a_transliteration_that_succeeds_is_the_one_used(self,
                                                                 monkeypatch):
        _stub_iconv(monkeypatch, 0, b"Amelie 2")
        assert tmdblookup.normalize_title("Amélie 2") == "amelie 2"

    def test_an_iconv_that_spells_the_accent_out_is_not_used(self,
                                                             monkeypatch):
        """The macOS rung. GNU libiconv answers "'e" where glibc answers "e",
        and a fold that took that would key "Amélie" as "am elie" - which
        stops matching the same film's ASCII spelling, so the whole reason
        the fold exists is gone."""
        monkeypatch.setattr(
            tmdblookup.subprocess, "run",
            lambda *_a, **_k: subprocess.CompletedProcess([], 0, b"'e", b""))
        tmdblookup.reset_iconv_flavour()
        assert tmdblookup.normalize_title("Amélie") == "amelie"

    def test_and_neither_is_a_host_with_no_iconv_at_all(self, monkeypatch):
        """Which used to be a FileNotFoundError out of the middle of a
        lookup."""
        def absent(*_a, **_k):
            raise FileNotFoundError(2, "no such file", "iconv")

        monkeypatch.setattr(tmdblookup.subprocess, "run", absent)
        tmdblookup.reset_iconv_flavour()
        assert tmdblookup.normalize_title("Café au lait") == "cafe au lait"

    @pytest.mark.parametrize("title,want", [
        ("Amélie", "amelie"),
        ("Übung 2: The Æra", "ubung 2 the aera"),
        ("Bàtman", "batman"),
        ("Mì Đội", "mi doi"),
        ("Straße", "strasse"),
        ("Blade Runner", "blade runner"),
    ])
    def test_the_python_fold_answers_what_glibc_answers(self, title, want):
        """The fallback is only a fallback while it agrees with the tool it
        stands in for: one library read on Linux and on a Mac has to fold to
        the same keys, or the same film reads as two."""
        folded = tmdblookup._fold_without_iconv(title)
        assert re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip() == want

    def test_the_flavour_is_settled_once_and_not_per_title(self, monkeypatch):
        """A lookup folds once per folder, and the answer cannot change under
        a running command."""
        calls = []

        def counted(*_a, input=None, **_k):
            calls.append(input)
            return subprocess.CompletedProcess(
                [], 0, tmdblookup._TRANSLIT_PROBE_GLIBC.encode("utf-8"), b"")

        monkeypatch.setattr(tmdblookup.subprocess, "run", counted)
        tmdblookup.reset_iconv_flavour()
        for _ in range(3):
            tmdblookup.normalize_title("Amélie")
        probe = tmdblookup._TRANSLIT_PROBE.encode("utf-8")
        assert calls.count(probe) == 1

    def test_empty_and_blank_fold_to_empty(self):
        assert tmdblookup.normalize_title("") == ""
        assert tmdblookup.normalize_title("   ") == ""
        assert tmdblookup.normalize_title("!!!") == ""


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
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""
        assert calls == []

    def test_an_unnormalisable_title_is_no_answer(self, monkeypatch):
        calls = _install(monkeypatch, None, {}, {})
        assert tmdblookup.tmdb_imdb_id("!!!", "1999") == ""
        assert calls == []

    def test_a_failed_search_is_no_answer(self, monkeypatch):
        calls = _install(monkeypatch, None, {}, {})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""
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
            _row(1, "Batman", date="1966-06-23"),
            _row(2, "Batman", date="2005-06-23")]})
        calls = _install(monkeypatch, search, {1: [], 2: []}, {1: "tt1"})
        assert tmdblookup.tmdb_imdb_id("Batman", "1989") == ""
        assert [url for url, _params in calls] == [
            _BASE + "/search/movie", _detail_url(1), _detail_url(2)]

    def test_zero_matching_titles_is_no_answer(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Not Batman")]})
        calls = _install(monkeypatch, search, {1: []}, {})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""
        # one candidate in the year, so its document was read
        assert [url for url, _params in calls] == [
            _BASE + "/search/movie", _detail_url(1)]

    def test_the_document_is_asked_for_in_one_call(self, monkeypatch):
        calls = _install(monkeypatch, json.dumps({"results": [_row(1, "Batman")]}),
                         {1: []}, {1: "tt0120737"})
        tmdblookup.tmdb_imdb_id("Batman", "1999")
        url, params = calls[1]
        assert url == _detail_url(1)
        assert params == [("api_key", "apikey"),
                          ("append_to_response",
                           "alternative_titles,external_ids,release_dates")]

    def test_a_single_match_returns_the_id(self, monkeypatch):
        search = json.dumps({"results": [
            _row(1, "Batman"), _row(2, "Other", date="1998-01-01")]})
        calls = _install(monkeypatch, search, {1: [], 2: []}, {1: "tt0120737"})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == "tt0120737"
        # the other release is near enough the year to be worth reading, and
        # turns out to carry a different title
        assert [url for url, _params in calls] == [
            _BASE + "/search/movie", _detail_url(1), _detail_url(2)]

    def test_two_matching_candidates_is_no_answer(self, monkeypatch):
        # the second carries the title only on its original title - the whole
        # point of the alternative/original scan
        search = json.dumps({"results": [
            _row(1, "Batman"), _row(2, "The Dark Knight", original="Batman")]})
        calls = _install(monkeypatch, search, {1: [], 2: []}, {1: "tt1"})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""
        assert [url for url, _params in calls] == [
            _BASE + "/search/movie", _detail_url(1), _detail_url(2)]

    def test_an_alternative_title_is_a_match(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "The Movie")]})
        _install(monkeypatch, search, {1: ["Batman"]}, {1: "tt0000001"})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == "tt0000001"

    def test_an_original_title_is_a_match(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "The Dark Knight",
                                              original="Batman")]})
        _install(monkeypatch, search, {1: []}, {1: "tt0000002"})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == "tt0000002"

    def test_a_missing_primary_is_the_word_null_not_a_match(self, monkeypatch):
        # jq -r prints the literal "null" for a missing title; it is compared
        # (and missed) like any other title, not skipped as if it were empty
        search = json.dumps({"results": [_row(1, None, original="Batman")]})
        _install(monkeypatch, search, {1: []}, {1: "tt0000003"})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == "tt0000003"

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
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""

    def test_a_non_tt_imdb_id_is_no_answer(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Batman")]})
        _install(monkeypatch, search, {1: []}, {1: "not-an-id"})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""

    def test_a_missing_imdb_id_is_no_answer(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Batman")]})
        _install(monkeypatch, search, {1: []}, {})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""

    def test_a_document_that_cannot_be_read_is_no_answer(self, monkeypatch):
        """The one call carries the titles, the id and the dates together, so a
        candidate whose document fails is a candidate nothing is known about -
        and nothing is what it matches on."""
        calls = []

        def fake_curl(url, _params):
            calls.append(url)
            if url == _BASE + "/search/movie":
                return json.dumps({"results": [_row(1, "Batman")]})
            return None  # the document call fails
        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
        monkeypatch.setenv("tmdbApiKey", "apikey")
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""
        assert calls[-1] == _detail_url(1)


class TestWhatTheLengthSettles:
    """The rule past the plain one: what a release year elsewhere answers for,
    and what the film's own length is - and is not - allowed to decide."""

    def test_a_release_year_elsewhere_answers_for_the_folder(self, monkeypatch):
        """The primary date is the premiere and the folder was named from the
        release a year later. One film, two true years, and no length needed to
        say so."""
        search = json.dumps({"results": [_row(1, "Batman", date="1998-09-01")]})
        _install(monkeypatch, search, {1: []}, {1: "tt0000001"},
                 years={1: ["1999"]})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == "tt0000001"

    def test_two_films_of_one_title_and_year_are_told_apart_by_length(
            self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Batman"), _row(2, "Batman")]})
        _install(monkeypatch, search, {1: [], 2: []}, {1: "tt1", 2: "tt2"},
                 runtimes={1: 90, 2: 150})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999",
                                       lambda: 150 * 60.0) == "tt2"

    def test_a_candidate_nobody_can_measure_keeps_it_uncertain(self, monkeypatch):
        """The film on the disk fits one of them, and the other states no
        runtime at all - so nothing rules it out, and one of two is still not
        an answer."""
        search = json.dumps({"results": [_row(1, "Batman"), _row(2, "Batman")]})
        _install(monkeypatch, search, {1: [], 2: []}, {1: "tt1", 2: "tt2"},
                 runtimes={1: 150})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999",
                                       lambda: 150 * 60.0) == ""

    def test_and_so_does_a_film_nothing_on_disk_can_measure(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Batman"), _row(2, "Batman")]})
        _install(monkeypatch, search, {1: [], 2: []}, {1: "tt1", 2: "tt2"},
                 runtimes={1: 90, 2: 150})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999", lambda: 0.0) == ""

    def test_a_length_that_fits_neither_is_no_answer(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Batman"), _row(2, "Batman")]})
        _install(monkeypatch, search, {1: [], 2: []}, {1: "tt1", 2: "tt2"},
                 runtimes={1: 90, 2: 150})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999",
                                       lambda: 200 * 60.0) == ""

    def test_a_year_off_by_one_is_named_when_the_length_agrees(self, monkeypatch):
        """No release of it happened in the year on the folder, so the year has
        been given up on - and the length is what stands in its place."""
        second = json.dumps({"results": [_row(1, "Batman", date="2000-06-23")]})
        _install(monkeypatch, json.dumps({"results": []}), {1: []},
                 {1: "tt0000009"}, runtimes={1: 120}, second=second)
        assert tmdblookup.tmdb_imdb_id("Batman", "1999",
                                       lambda: 121 * 60.0) == "tt0000009"

    def test_and_is_not_named_without_a_length_to_agree(self, monkeypatch):
        second = json.dumps({"results": [_row(1, "Batman", date="2000-06-23")]})
        _install(monkeypatch, json.dumps({"results": []}), {1: []},
                 {1: "tt0000009"}, runtimes={1: 120}, second=second)
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""

    def test_nor_when_the_length_disagrees(self, monkeypatch):
        second = json.dumps({"results": [_row(1, "Batman", date="2000-06-23")]})
        _install(monkeypatch, json.dumps({"results": []}), {1: []},
                 {1: "tt0000009"}, runtimes={1: 120}, second=second)
        assert tmdblookup.tmdb_imdb_id("Batman", "1999",
                                       lambda: 90 * 60.0) == ""

    def test_a_year_further_off_than_one_is_not_reached_by_a_length(
            self, monkeypatch):
        second = json.dumps({"results": [_row(1, "Batman", date="2003-06-23")]})
        _install(monkeypatch, json.dumps({"results": []}), {1: []},
                 {1: "tt0000009"}, runtimes={1: 120}, second=second)
        assert tmdblookup.tmdb_imdb_id("Batman", "1999",
                                       lambda: 120 * 60.0) == ""

    def test_the_disk_is_not_read_when_the_year_settled_it(self, monkeypatch):
        """The probe costs an ffprobe of every film in the library, so it is
        asked for only by the ones the plain rule could not name."""
        search = json.dumps({"results": [_row(1, "Batman")]})
        _install(monkeypatch, search, {1: []}, {1: "tt0000001"})
        asked = []

        def measure():
            asked.append(True)
            return 7200.0
        assert tmdblookup.tmdb_imdb_id("Batman", "1999", measure) == "tt0000001"
        assert asked == []

    def test_nor_when_there_is_nothing_left_to_settle(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Not Batman")]})
        _install(monkeypatch, search, {1: []}, {1: "tt0000001"})
        asked = []

        def measure():
            asked.append(True)
            return 7200.0
        assert tmdblookup.tmdb_imdb_id("Batman", "1999", measure) == ""
        assert asked == []

    def test_only_the_first_handful_of_results_cost_a_request(self, monkeypatch):
        """Twenty answers to one title is TMDb saying it does not know which;
        every one of them would be a request, and the ones past the first few
        are not what a certainty rule turns on."""
        search = json.dumps({"results": [_row(n, "Batman")
                                         for n in range(1, 21)]})
        calls = _install(monkeypatch, search, {}, {})
        tmdblookup.tmdb_imdb_id("Batman", "1999")
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


class TestTitleKeys:
    """A title carrying an apostrophe is matched by both readings of it.

    normalize_title collapses each run of punctuation to a SPACE, which is
    right for a dash and wrong for an apostrophe: a library whose names have
    had their apostrophes stripped would otherwise never meet the same title
    on TMDb.
    """

    def test_a_title_with_no_apostrophe_costs_no_second_fold(self):
        assert tmdblookup.title_keys("The Movie") == {"the movie"}

    def test_both_readings_are_offered_for_one_that_has_one(self):
        assert tmdblookup.title_keys("A Cats Owners Tale".replace("s ", "'s ")) \
            == {"a cat s owner s tale", "a cats owners tale"}

    @pytest.mark.parametrize("written,stripped", [
        ("A Cat's Tale", "A Cats Tale"),
        ("A Cat\u2019s Tale", "A Cats Tale"),
        ("L'Animal", "LAnimal"),
    ])
    def test_the_two_spellings_of_one_title_meet(self, written, stripped):
        assert tmdblookup.title_keys(written) & tmdblookup.title_keys(stripped)

    def test_a_title_that_matched_before_still_matches(self):
        """Widening only ever ADDS keys."""
        assert tmdblookup.normalize_title("The Movie") \
            in tmdblookup.title_keys("The Movie")

    def test_two_different_titles_still_do_not_meet(self):
        assert not (tmdblookup.title_keys("A Cat's Tale")
                    & tmdblookup.title_keys("A Dog's Tale"))


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
        body = [line for line in open(path, encoding="utf-8").read().splitlines()
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
        calls = self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        ambiguous: list = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog(),
                                ambiguous=ambiguous)
        # left exactly as it was, and not even looked up
        assert sorted(p.name for p in (tmp_path / "The Movie (1934)").iterdir()) \
            == ["The Movie (1934) Part 1 - The First Half.en.srt",
                "The Movie (1934) Part 1 - The First Half.mkv",
                "The Movie (1934) Part 2 - The Others.mkv"]
        assert calls == []
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
        # "Batman (1999) (2005)": the greedy title takes up to the LAST year, so
        # the film is "Batman (1999)" of 2005, and that is what is searched
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Batman (1999) (2005)", ["Batman (1999) (2005).mkv"]))
        self._env(monkeypatch, {"Batman (1999)": "tt0000009"}, base="2005-05-01")
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        assert (tmp_path / "Batman (1999) (2005) {imdb-tt0000009}").is_dir()
        assert logs == ['  match: "Batman (1999) (2005)" -> {imdb-tt0000009}']

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