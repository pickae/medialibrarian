"""Tests for medialib.lib.dvdcompare - reading the site's pages, finding a film's
page, and asking the site politely.

The two pages under `tests/data/dvdcompare/` are written for these cases in the
site's own markup, broken nesting included, about an invented film: what is
pinned is which lines of a release are the film's commentary tracks, on which
kind of disc, and which are not - a featurette's optional commentary, a scene's,
a box set's other film.
"""

import os
import time

import pytest

from medialib.lib import dvdcompare, politepacing
from medialib.lib.commentarynames import Disc
from tests import blackbox

pytestmark = pytest.mark.pure

_DATA = blackbox.DATA / "dvdcompare"
FILM = (_DATA / "film.html").read_text(encoding="utf-8")
SEARCH = (_DATA / "search.html").read_text(encoding="utf-8")

DIRECTOR = "Audio commentary by director Wenna Castellane"
CAST = "Audio commentary with actors Oriel Pask and Tamsin Voller"


class TestThePictureFormat:
    """The kind of disc a release's "Picture Format" names: the resolution is
    what says it, and a format the site names without a resolution is a DVD."""

    def test_a_2160_picture_is_a_4k_disc(self):
        assert dvdcompare._picture_kind("2160p") == "uhd"

    def test_and_1080_or_720_a_blu_ray(self):
        assert dvdcompare._picture_kind("1080p") == "bluray"
        assert dvdcompare._picture_kind("720p") == "bluray"

    def test_a_std_def_576i_is_a_dvd(self):
        assert dvdcompare._picture_kind("576i") == "dvd"

    def test_and_no_format_says_none(self):
        assert dvdcompare._picture_kind("") == ""


class TestAFilmsPage:
    def test_every_release_is_read_and_the_page_heading_is_not(self):
        labels = [release.label for release in dvdcompare.parse_film_page(FILM)]
        assert labels == [
            "Blu-ray A America - Invented Pictures [2011 Release]",
            "Blu-ray B United Kingdom - Made Up Media [2024 Release] "
            "Collector's Edition",
            "Blu-ray ALL Germany - Fiction Film [2012 Release]"]

    def test_a_commentaries_block_is_the_release_s_own_disc(self):
        first = dvdcompare.parse_film_page(FILM)[0]
        assert first.discs == (Disc("bluray", (DIRECTOR, CAST)),)

    def test_extras_are_read_disc_by_disc_and_cut_by_cut(self):
        second = dvdcompare.parse_film_page(FILM)[1]
        assert second.discs == (
            Disc("bluray", (CAST, DIRECTOR)),
            Disc("bluray",
                 ("NEW Audio commentary by cinematographer Ludo Harrowgate",)),
            Disc("bluray", ("Selected Scene Audio Commentary by critic Ivo "
                            "Brandt (38:43)",)),
            Disc("dvd", (DIRECTOR,)))

    def test_a_box_set_s_other_film_is_not_this_one(self):
        second = dvdcompare.parse_film_page(FILM)[1]
        listed = [entry for disc in second.discs for entry in disc.commentaries]
        assert "Audio commentary by critic Ivo Brandt" not in listed

    def test_a_release_with_no_commentary_lists_none(self):
        assert dvdcompare.parse_film_page(FILM)[2].discs == ()

    def test_a_page_that_is_not_a_film_s_lists_nothing(self):
        assert dvdcompare.parse_film_page(SEARCH) == []
        assert dvdcompare.parse_film_page("") == []


class TestTheSearch:
    def test_every_film_offered_is_read(self):
        entries = dvdcompare.parse_search(SEARCH)
        assert entries[0] == dvdcompare.SearchEntry(
            "11", ("Nightfall Harbour", "Hafen der Nacht"), "dvd", "1987",
            False)
        assert [(e.fid, e.kind, e.tv) for e in entries[1:4]] == [
            ("12", "bluray", False), ("13", "uhd", False),
            ("14", "bluray", True)]

    def test_an_article_filed_at_the_end_is_put_back(self):
        entries = dvdcompare.parse_search(SEARCH)
        assert ("The Lighthouse: The Last Keeper",) in [e.names for e in entries]

    @pytest.mark.parametrize("title, kind, fid", [
        ("Nightfall Harbour", "bluray", "12"),
        ("Nightfall Harbour", "dvd", "11"),
        ("Hafen der Nacht", "bluray", "12"),
        ("The Lighthouse: The Last Keeper", "bluray", ""),
    ])
    def test_the_page_is_picked_by_title_year_and_kind(self, title, kind, fid):
        entries = dvdcompare.parse_search(SEARCH)
        assert dvdcompare.pick(entries, title, "1987", kind) == fid

    def test_a_year_either_side_is_taken_when_none_is_exact(self):
        entries = dvdcompare.parse_search(SEARCH)
        assert dvdcompare.pick(entries, "Nightfall Harbour", "1987", "uhd") \
            == "13"

    def test_television_is_never_the_film(self):
        entries = [e for e in dvdcompare.parse_search(SEARCH) if e.fid != "12"]
        assert dvdcompare.pick(entries, "Nightfall Harbour", "1987",
                               "bluray") == ""

    def test_two_pages_that_both_fit_are_no_answer(self):
        entries = dvdcompare.parse_search(SEARCH)
        assert dvdcompare.pick(entries, "Twin Harbour", "1995", "bluray") == ""

    @pytest.mark.parametrize("title, query", [
        ("The Lighthouse: The Last Keeper", "Lighthouse"),
        ("Nightfall Harbour", "Nightfall Harbour"),
        ("A Night - The Harbour", "Night"),
        ("The", "The"),
    ])
    def test_the_search_is_for_the_name_as_the_site_files_it(self, title,
                                                              query):
        assert dvdcompare.search_query(title) == query


class _Clock:
    def __init__(self):
        self.time = 100.0
        self.slept = []

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.time += seconds


class TestAskingTheSite:
    @pytest.fixture
    def site(self, tmp_path):
        asked = []
        clock = _Clock()

        def fetch(url, form):
            asked.append((url, form))
            return SEARCH if url.endswith("search.php") else FILM

        pacer = politepacing.Pacer(dvdcompare.MIN_INTERVAL, dvdcompare.JITTER,
                                   now=clock.now, sleep=clock.sleep,
                                   draw=lambda low, high: high)
        made = dvdcompare.Site(str(tmp_path), lambda _line: None, fetch, pacer)
        return made, asked, clock

    def test_the_search_is_posted_the_way_the_site_s_form_posts_it(self, site):
        made, asked, _clock = site
        made.releases("Nightfall Harbour", "1987", "dvd")
        assert asked[0] == (dvdcompare.BASE + "search.php",
                            {"param": "Nightfall Harbour",
                             "searchtype": "text"})
        assert asked[1] == (dvdcompare.BASE + "film.php?fid=11", None)

    def test_a_blu_ray_is_looked_for_on_the_4k_page_as_well(self, site):
        made, asked, _clock = site
        made.releases("Nightfall Harbour", "1987", "bluray")
        assert [url for url, _form in asked[1:]] == [
            dvdcompare.BASE + "film.php?fid=12",
            dvdcompare.BASE + "film.php?fid=13"]

    def test_requests_are_spaced_out(self, site):
        made, _asked, clock = site
        made.releases("Nightfall Harbour", "1987", "bluray")
        assert clock.slept == [dvdcompare.MIN_INTERVAL + dvdcompare.JITTER] * 2

    def test_a_page_asked_for_once_is_not_asked_for_again(self, site,
                                                          tmp_path):
        made, asked, _clock = site
        made.releases("Nightfall Harbour", "1987", "dvd")
        again = dvdcompare.Site(str(tmp_path), lambda _line: None,
                                lambda *a: asked.append(a))
        assert again.releases("Nightfall Harbour", "1987", "dvd")
        assert len(asked) == 2

    def test_a_stale_cached_page_is_fetched_again(self, site, tmp_path):
        """A page is believed only while it is young: the one a run kept, once
        it is older than the belief window, is not trusted and is asked for
        again rather than read from where it sits."""
        made, asked, _clock = site
        made.releases("Nightfall Harbour", "1987", "dvd")
        first = len(asked)
        # the pages it kept are now far older than they are believed to stay
        long_ago = time.time() - 2 * 365 * 86400
        for page in tmp_path.glob("*.html"):
            os.utime(str(page), (long_ago, long_ago))

        def refetch(url, form):
            asked.append((url, form))
            return SEARCH if url.endswith("search.php") else FILM

        again = dvdcompare.Site(
            str(tmp_path), lambda _line: None, refetch,
            pacer=politepacing.Pacer(
                dvdcompare.MIN_INTERVAL, dvdcompare.JITTER,
                now=_Clock().now, sleep=lambda _s: None,
                draw=lambda _low, high: high))
        assert again.releases("Nightfall Harbour", "1987", "dvd")
        assert len(asked) > first

    def test_a_film_the_search_cannot_place_is_none(self, site):
        made, _asked, _clock = site
        assert made.releases("Twin Harbour", "1995", "bluray") is None

    def test_a_site_that_does_not_answer_is_none(self, tmp_path):
        made = dvdcompare.Site(str(tmp_path), lambda _line: None,
                               lambda *a: None)
        assert made.releases("Nightfall Harbour", "1987", "dvd") is None

    def test_a_site_that_does_not_answer_is_not_asked_again(self, tmp_path):
        asked = []
        made = dvdcompare.Site(str(tmp_path), lambda _line: None,
                               lambda *a: asked.append(a))
        made.releases("Nightfall Harbour", "1987", "dvd")
        made.releases("Twin Harbour", "1995", "dvd")
        assert len(asked) == 1
