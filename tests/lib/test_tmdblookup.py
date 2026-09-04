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


def _install(monkeypatch, search, alt_titles, ext_ids, key="apikey"):
    """Stand the network in with canned answers and record the calls.

    ``search`` is the raw search body (or None for a curl that fails),
    ``alt_titles`` maps a candidate id to the titles its alternative_titles call
    returns, and ``ext_ids`` maps the one match's id to its imdb_id (absent for
    none).
    """
    monkeypatch.setenv("tmdbApiKey", key)
    calls = []

    def fake_curl(url, params):
        calls.append((url, list(params)))
        if url == _BASE + "/search/movie":
            return search
        match = re.match(r"^" + re.escape(_BASE)
                         + r"/movie/(\d+)/(alternative_titles|external_ids)$",
                         url)
        if match:
            key_id, kind = int(match.group(1)), match.group(2)
            if kind == "alternative_titles":
                return json.dumps({"titles": [{"title": t}
                                              for t in alt_titles.get(key_id, [])]})
            return json.dumps({"imdb_id": ext_ids.get(key_id)})
        return None

    monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
    return calls


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
        (url, params), = calls
        assert url == _BASE + "/search/movie"
        assert params == [("api_key", "apikey"), ("query", "Some Movie"),
                          ("year", "2001"), ("include_adult", "false")]

    def test_no_candidate_in_the_requested_year(self, monkeypatch):
        search = json.dumps({"results": [
            _row(1, "Batman", date="1998-06-23"),
            _row(2, "Batman", date="2000-06-23")]})
        calls = _install(monkeypatch, search, {}, {})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""
        # the year filter dropped both, so no per-candidate call was made
        assert [c[0] for c in calls] == [_BASE + "/search/movie"]

    def test_zero_matching_titles_is_no_answer(self, monkeypatch):
        search = json.dumps({"results": [_row(1, "Not Batman")]})
        calls = _install(monkeypatch, search, {1: []}, {})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""
        # one candidate in the year, so it was asked for alternatives
        assert [c[0] for c in calls] == [
            _BASE + "/search/movie", _BASE + "/movie/1/alternative_titles"]

    def test_a_single_match_returns_the_id(self, monkeypatch):
        search = json.dumps({"results": [
            _row(1, "Batman"), _row(2, "Other", date="1998-01-01")]})
        calls = _install(monkeypatch, search, {1: []}, {1: "tt0120737"})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == "tt0120737"
        assert calls[-1][0] == _BASE + "/movie/1/external_ids"

    def test_two_matching_candidates_is_no_answer(self, monkeypatch):
        # the second carries the title only on its original title - the whole
        # point of the alternative/original scan
        search = json.dumps({"results": [
            _row(1, "Batman"), _row(2, "The Dark Knight", original="Batman")]})
        calls = _install(monkeypatch, search, {1: [], 2: []}, {1: "tt1"})
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""
        # both candidates in the year were asked for alternatives, and neither
        # external_ids call was made because the match was not unique
        assert [c[0] for c in calls] == [
            _BASE + "/search/movie",
            _BASE + "/movie/1/alternative_titles",
            _BASE + "/movie/2/alternative_titles"]

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
        # the alternative list has an entry with no title at all
        monkeypatch.setenv("tmdbApiKey", "apikey")
        calls = []

        def fake_curl(url, params):
            calls.append(url)
            if url == _BASE + "/search/movie":
                return json.dumps({"results": [_row(1, "The Movie")]})
            if url == _BASE + "/movie/1/alternative_titles":
                return json.dumps({"titles": [{}]})  # no title key at all
            if url == _BASE + "/movie/1/external_ids":
                return json.dumps({"imdb_id": "tt0000004"})
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

    def test_a_failed_external_ids_call_is_no_answer(self, monkeypatch):
        calls = []

        def fake_curl(url, params):
            calls.append(url)
            if url == _BASE + "/search/movie":
                return json.dumps({"results": [_row(1, "Batman")]})
            if url == _BASE + "/movie/1/alternative_titles":
                return json.dumps({"titles": []})
            return None  # the external_ids call fails
        monkeypatch.setattr(tmdblookup, "_curl", fake_curl)
        monkeypatch.setenv("tmdbApiKey", "apikey")
        assert tmdblookup.tmdb_imdb_id("Batman", "1999") == ""
        assert calls[-1] == _BASE + "/movie/1/external_ids"


# --- tagging a folder tree ----------------------------------------------------


def _tree(root, *folders):
    """A library of movie folders: each (base, fileNames...) becomes a folder
    holding the named files."""
    for spec in folders:
        base, files = spec
        (root / base).mkdir(parents=True)
        for name in files:
            (root / base / name).touch()


class TestTagPlexIds:
    def _env(self, monkeypatch, matches, base="1999-06-23"):
        """Stand the network so that, for each title in ``matches`` that
        resolves to an id, its search returns one same-year same-title result
        and its external_ids returns that id; every other title is a miss."""
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
            match = re.match(r"^" + re.escape(_BASE) + r"/movie/(\d+)/external_ids$", url)
            if match:
                rid = int(match.group(1))
                for _t, (r2, imdb) in by_title.items():
                    if r2 == rid:
                        return json.dumps({"imdb_id": imdb})
            return json.dumps({"titles": []})
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

    def test_a_folder_tmdB_rejects_is_left_alone(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _tree(tmp_path, ("Unknown Film (2010)", ["Unknown Film (2010).mkv"]))
        self._env(monkeypatch, {"Unknown Film": None})
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        assert logs == []
        assert (tmp_path / "Unknown Film (2010)/Unknown Film (2010).mkv").is_file()

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

    def test_a_symlinked_folder_is_not_tagged(self, monkeypatch, tmp_path):
        # find -P -type d: a link to a movie folder is a link, not a movie
        monkeypatch.chdir(tmp_path)
        real = tmp_path / "Real"
        _tree(real, ("The Movie (1999)", ["The Movie (1999).mkv"]))
        (tmp_path / "Link").symlink_to("Real")
        self._env(monkeypatch, {"The Movie": "tt0120737"})
        logs = []
        tmdblookup.tag_plex_ids(".", logs.append, SkipLog())
        # the link is not descended through; the real tree is not under "."
        assert logs == []
        assert (tmp_path / "Link").is_symlink()

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