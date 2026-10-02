"""Tests for medialib.lib.chapterdb - chapters for a film, from the archive of
chapter sets read off discs.

Nothing here reaches the archive: every page and set is handed in through the
``fetch`` the lookup takes. What is under test is the JUDGEMENT - which set is the
disc a film was ripped from, and what a film's own chapters may be replaced by -
because a set from the wrong cut lands every mark in the wrong scene, and does
so without any error to see.
"""

import os

import pytest

from medialib.lib import chapterdb

pytestmark = pytest.mark.pure

FILM = 8177.96  # two hours sixteen, the length of the film the sets are for


def _set(chapters, seconds=FILM, language="eng", confirmations=10,
         set_id="1", source="Blu-Ray"):
    return chapterdb.ChapterSet(set_id, "Nordwind", source, language,
                                confirmations, seconds, tuple(chapters))


NAMED = [(0.0, "Opening"), (900.0, "The Harbour"), (3000.0, "Storm"),
         (6000.0, "Landfall")]
NUMBERED = [(0.0, "Chapter 1"), (900.0, "Chapter 2"), (3000.0, "Chapter 3"),
            (6000.0, "Chapter 4")]


def _numbered_film(starts=(0.0, 900.0, 3000.0, 6000.0)):
    """A film whose rip kept the disc's marks and numbered them."""
    return chapterdb.Existing(FILM, tuple("Chapter %02d" % number for number
                                          in range(1, len(starts) + 1)),
                              tuple(starts))


def _row(set_id, title, stamp, confirmations="5", source="Blu-Ray"):
    return ('<tr><td style="width:9%%"><span>%s</span></td>'
            '<td style="width:7%%">%s</td>'
            '<td><a href="/browse/%s">%s</a></td>'
            '<td style="width:12%%">%s</td>'
            '<td>someone</td></tr>' % (confirmations, source, set_id, title,
                                       stamp))


def _xml(chapters, seconds="02:16:17.9614444", confirmations="12",
         language="eng"):
    rows = "".join('<chapter time="%s" name="%s" />' % pair
                   for pair in chapters)
    return ('<chapterInfo xml:lang="%s" confirmations="%s" '
            'xmlns="http://example.test/chapters">'
            '<title>Nordwind</title><source><type>Blu-Ray</type>'
            '<duration>%s</duration></source><chapters>%s</chapters>'
            '</chapterInfo>' % (language, confirmations, seconds, rows))


class TestWhatCountsAsANumberedName:
    """A chapter name that says nothing but where it is in the list is not a
    name, in any of the six languages - and a real name that merely starts
    with a number, or is spelled in the letters of one, still is."""

    @pytest.mark.parametrize("name", [
        "", "Chapter 1", "Chapter 01", "chapter 12.", "Kapitel 3",
        "Chapitre IV", "Capítulo 2", "Hoofdstuk 7", "Scene 5", "07", "12.",
        "00:05:00.000", "1:23:45"])
    def test_a_number_is_not_a_name(self, name):
        assert chapterdb.Existing(FILM, (name,)).kind == "numbered"

    @pytest.mark.parametrize("name", [
        "Lanterns in the Fog", "Chapter 1: The Arrival", "Mix", "Civil",
        "Ivan"])
    def test_a_name_is_a_name(self, name):
        """"Mix" is made of the letters of a roman numeral without being one."""
        assert chapterdb.Existing(FILM, (name,)).kind == "named"

    def test_a_bare_year_counts_as_a_number(self):
        """The side to err on: a chapter named only "1999" is numbered to the
        rule, and a numbered chapter is only ever replaced by names."""
        assert chapterdb.Existing(FILM, ("1999",)).kind == "numbered"

    def test_no_chapters_is_none(self):
        assert chapterdb.Existing(FILM, ()).kind == "none"

    def test_one_real_name_among_numbers_is_somebodys_work(self):
        """Left alone: half-named chapters were named by hand."""
        assert chapterdb.Existing(FILM, ("Chapter 1", "The Harbour")).kind \
            == "named"

    def test_a_set_with_a_repeated_name_is_not_named(self):
        assert not chapterdb.is_named(["Storm", "Harbour", "storm"])

    def test_a_set_that_numbers_some_is_not_named(self):
        assert not chapterdb.is_named(["Opening", "Chapter 2", "Storm"])


class TestReadingTheArchive:

    def test_a_search_row_is_read_with_its_duration(self):
        page = _row("104", "Nordwind", "02:16.17") + _row(
            "105", "Salt &amp; Iron", "00:32.05")
        rows = chapterdb.parse_search(page)
        assert rows == [
            chapterdb.Row("104", "Nordwind", "Blu-Ray", 8177),
            chapterdb.Row("105", "Salt & Iron", "Blu-Ray", 1925)]

    def test_a_set_is_read_whole(self):
        parsed = chapterdb.parse_set("7", _xml([("00:00:00", "Opening"),
                                                ("00:15:00.5", "Harbour")]))
        assert parsed.set_id == "7"
        assert parsed.language == "eng"
        assert parsed.confirmations == 12
        assert parsed.seconds == pytest.approx(8177.9614444)
        assert parsed.chapters == ((0.0, "Opening"), (900.5, "Harbour"))

    def test_a_page_that_is_not_a_set_is_none(self):
        assert chapterdb.parse_set("7", "<html>Attention Required</html>") \
            is None
        assert chapterdb.parse_set("7", "not xml at all <") is None

    def test_a_search_reads_on_until_a_short_page(self, monkeypatch):
        monkeypatch.setattr(chapterdb, "PAGE_SIZE", 2)
        pages = {1: _row("1", "A", "01:00.00") + _row("2", "A", "01:00.00"),
                 2: _row("3", "A", "01:00.00")}
        asked = []

        def fetch(path, params=()):
            page = dict(params)["page"]
            asked.append(page)
            return pages.get(page, "")
        assert [row.set_id for row in chapterdb.search("A", fetch)] == \
            ["1", "2", "3"]
        assert asked == [1, 2]

    def test_an_archive_that_does_not_answer_is_not_no_rows(self):
        assert chapterdb.search("A", lambda path, params=(): None) is None

    def test_a_disc_label_is_read_as_its_title(self):
        assert chapterdb.same_title("Falcons Forever Rising",
                                    "Falcons.Forever.Rising.2007.EXTENDED")
        assert chapterdb.same_title("Nordwind", "Nordwind (1998) HDDVD")
        assert not chapterdb.same_title("Nordwind", "Nordwind Returns")


class TestTheSniffTest:
    """What a set must be before a film is given it."""

    def test_the_right_set_passes(self):
        assert chapterdb.plausible(_set(NAMED), FILM) == ""

    def test_another_cut_is_refused_by_its_length(self):
        """An extended cut, or a PAL transfer 4% fast: minutes, not seconds."""
        assert "runs" in chapterdb.plausible(_set(NAMED, FILM + 600), FILM)
        assert "runs" in chapterdb.plausible(_set(NAMED, FILM * 0.96), FILM)

    def test_two_seconds_either_way_is_the_same_disc(self):
        assert chapterdb.plausible(_set(NAMED, FILM + 1.9), FILM) == ""
        assert chapterdb.plausible(_set(NAMED, FILM - 1.9), FILM) == ""
        assert chapterdb.plausible(_set(NAMED, FILM + 2.1), FILM) != ""

    def test_a_set_with_no_duration_cannot_be_checked(self):
        assert chapterdb.plausible(_set(NAMED, 0.0), FILM) == \
            "states no duration"

    def test_too_few_chapters(self):
        assert "chapter(s)" in chapterdb.plausible(_set(NAMED[:2]), FILM)

    def test_a_set_that_does_not_start_at_the_start(self):
        late = [(60.0, "Harbour")] + NAMED[1:]
        assert chapterdb.plausible(_set(late), FILM) == \
            "does not start at the start"

    def test_a_set_out_of_order(self):
        jumbled = [NAMED[0], NAMED[2], NAMED[1], NAMED[3]]
        assert chapterdb.plausible(_set(jumbled), FILM) == "is not in order"

    def test_a_set_bunched_at_the_start(self):
        bunched = [(0.0, "A"), (60.0, "B"), (120.0, "C")]
        assert chapterdb.plausible(_set(bunched), FILM) == \
            "has every chapter in the first half"

    def test_a_mark_past_the_end(self):
        past = NAMED + [(FILM + 30, "After")]
        assert chapterdb.plausible(_set(past), FILM) == \
            "has a chapter past the end"

    @pytest.mark.parametrize("stamp", ["00:15", "abc", ""])
    def test_a_time_that_cannot_be_read_refuses_the_set(self, stamp):
        """Never a mark at 0: the first chapter is the one place a 0 would
        pass every other check."""
        downloaded = chapterdb.parse_set("7", _xml(
            [(stamp, "Opening"), ("00:15:00", "The Harbour"),
             ("00:50:00", "Storm"), ("01:40:00", "Landfall")]))
        assert chapterdb.plausible(downloaded, FILM) == "has a malformed time"
        assert chapterdb.choose([downloaded],
                                chapterdb.Existing(FILM, ())) is None


class TestWhichSetAFilmIsGiven:

    def test_a_film_without_chapters_prefers_names(self):
        numbered = _set(NUMBERED, confirmations=90, set_id="n")
        named = _set(NAMED, confirmations=3, set_id="m")
        chosen = chapterdb.choose([numbered, named],
                                  chapterdb.Existing(FILM, ()))
        assert chosen.set_id == "m"

    def test_a_film_without_chapters_takes_numbered_ones_when_that_is_all(self):
        chosen = chapterdb.choose([_set(NUMBERED)],
                                  chapterdb.Existing(FILM, ()))
        assert chosen is not None and not chosen.named

    def test_numbered_chapters_are_only_replaced_by_named_ones(self):
        assert chapterdb.choose([_set(NUMBERED)], _numbered_film()) is None
        assert chapterdb.choose([_set(NAMED)], _numbered_film()).named

    def test_the_same_marks_a_frame_or_a_rounding_apart_are_replaced(self):
        film = _numbered_film((0.0, 900.04, 2999.2, 6000.9))
        assert chapterdb.choose([_set(NAMED)], film).named

    def test_numbered_chapters_at_other_times_are_kept(self):
        """The whole point of replacing them is the names. A set that moves
        a single mark by more than a second is another authoring, and the
        film's own marks are the ones known to be right for it."""
        film = _numbered_film((0.0, 900.0, 3002.0, 6000.0))
        assert chapterdb.choose([_set(NAMED)], film) is None

    def test_numbered_chapters_every_five_minutes_are_kept(self):
        """What a muxer generates is not the disc's marks, and no named set
        will ever line up with them."""
        film = _numbered_film(tuple(float(start)
                                    for start in range(0, 8100, 300)))
        assert chapterdb.choose([_set(NAMED)], film) is None

    def test_a_different_number_of_chapters_is_kept(self):
        assert chapterdb.choose([_set(NAMED)],
                                _numbered_film((0.0, 900.0, 3000.0))) is None
        assert chapterdb.choose([_set(NAMED)], _numbered_film(
            (0.0, 900.0, 3000.0, 6000.0, 7000.0))) is None

    def test_a_closing_mark_on_either_side_is_not_counted(self):
        film = _numbered_film((0.0, 900.0, 3000.0, 6000.0, FILM - 0.4))
        assert chapterdb.choose([_set(NAMED)], film).named
        closing = NAMED + [(FILM - 0.2, "End")]
        assert chapterdb.choose([_set(closing)], _numbered_film()).named

    def test_named_chapters_are_left_alone(self):
        existing = chapterdb.Existing(FILM, ("Opening", "Storm"))
        assert chapterdb.choose([_set(NAMED)], existing) is None

    def test_english_then_confirmations_then_length(self):
        german = _set(NAMED, language="ger", confirmations=99, set_id="de")
        few = _set(NAMED, confirmations=2, set_id="few")
        many_far = _set(NAMED, FILM + 1.5, confirmations=40, set_id="far")
        many_near = _set(NAMED, FILM + 0.1, confirmations=40, set_id="near")
        chosen = chapterdb.choose([german, few, many_far, many_near],
                                  chapterdb.Existing(FILM, ()))
        assert chosen.set_id == "near"

    def test_an_empty_closing_mark_is_dropped_not_refused(self):
        closing = NAMED + [(FILM - 0.3, "End")]
        chosen = chapterdb.choose([_set(closing)], chapterdb.Existing(FILM, ()))
        assert [name for _start, name in chosen.chapters] == \
            [name for _start, name in NAMED]

    def test_a_set_of_another_cut_is_never_chosen(self):
        assert chapterdb.choose([_set(NAMED, FILM + 300)],
                                chapterdb.Existing(FILM, ())) is None


class TestTheChapterFile:

    def test_named_chapters_keep_their_names_escaped(self):
        text = chapterdb.chapters_xml(_set(
            [(0.0, 'The "Kid" & Me'), (3725.5, "B"), (5000.0, "C")]))
        assert "<ChapterString>The \"Kid\" &amp; Me</ChapterString>" in text
        assert "<ChapterTimeStart>01:02:05.500000000</ChapterTimeStart>" in text
        assert text.count("<ChapterLanguage>eng</ChapterLanguage>") == 3

    def test_numbered_chapters_are_numbered_alike(self):
        text = chapterdb.chapters_xml(_set(
            [(0.0, "1"), (100.0, "Chapter 2"), (200.0, "chap. 3")]))
        assert "<ChapterString>Chapter 01</ChapterString>" in text
        assert "<ChapterString>Chapter 03</ChapterString>" in text


@pytest.mark.fs
class TestAddingChaptersToALibrary:
    """The walk, with the probing and the writing stood in for."""

    @pytest.fixture
    def library(self, tmp_path):
        for folder in ("Nordwind (1998) {imdb-tt0000001}", "Untagged (2001)",
                       "Named (2003) {imdb-tt0000003}",
                       "Nordwind (1998) {imdb-tt0000001}/Featurettes"):
            (tmp_path / folder).mkdir()
        (tmp_path / "Nordwind (1998) {imdb-tt0000001}" /
         "Nordwind (1998) {imdb-tt0000001}.mkv").write_bytes(b"")
        (tmp_path / "Nordwind (1998) {imdb-tt0000001}" /
         "Nordwind (1998) {imdb-tt0000001} (old).mkv").write_bytes(b"")
        (tmp_path / "Nordwind (1998) {imdb-tt0000001}" / "Featurettes" /
         "Making of.mkv").write_bytes(b"")
        (tmp_path / "Untagged (2001)" / "Untagged (2001).mkv").write_bytes(b"")
        (tmp_path / "Named (2003) {imdb-tt0000003}" /
         "Named (2003) {imdb-tt0000003}.mkv").write_bytes(b"")
        return tmp_path

    @pytest.fixture
    def probed(self, monkeypatch):
        def existing(movie):
            if "Named" in movie:
                return chapterdb.Existing(FILM, ("Opening", "Storm"))
            return _numbered_film()
        monkeypatch.setattr(chapterdb, "existing_chapters", existing)
        written = []
        monkeypatch.setattr(chapterdb, "write_chapters",
                            lambda movie, chosen: written.append(
                                (os.path.basename(movie), chosen.set_id))
                            or True)
        return written

    @staticmethod
    def _archive(asked):
        def fetch(path, params=()):
            asked.append((path, dict(params).get("title")))
            if path == "/browse":
                return _row("42", "Nordwind", "02:16.17")
            return _xml([(stamp, name) for stamp, name in (
                ("00:00:00", "Opening"), ("00:15:00", "The Harbour"),
                ("00:50:00", "Storm"), ("01:40:00", "Landfall"))])
        return fetch

    def test_only_the_tagged_feature_is_looked_up_and_written(self, library,
                                                              probed):
        asked, lines = [], []
        outcome = chapterdb.add_chapters(str(library), lines.append,
                                         self._archive(asked))
        assert probed == [("Nordwind (1998) {imdb-tt0000001}.mkv", "42")]
        assert asked == [("/browse", "Nordwind"), ("/browse/42.xml", None)]
        assert len(outcome["replaced"]) == 1
        assert len(outcome["kept"]) == 1
        assert len(outcome["untagged"]) == 1

    def test_numbered_marks_the_archive_does_not_share_are_kept(
            self, library, probed, monkeypatch):
        monkeypatch.setattr(chapterdb, "existing_chapters",
                            lambda movie: _numbered_film((0.0, 1200.0,
                                                          3000.0, 6000.0)))
        outcome = chapterdb.add_chapters(str(library), [].append,
                                         self._archive([]))
        assert probed == []
        assert len(outcome["unmatched"]) == 2

    def test_an_archive_that_does_not_answer_stops_the_lookups(self, library,
                                                               probed):
        lines = []
        outcome = chapterdb.add_chapters(str(library), lines.append,
                                         lambda path, params=(): None)
        assert probed == []
        assert any("did not answer" in line for line in lines)
        assert outcome["unmatched"] == []
