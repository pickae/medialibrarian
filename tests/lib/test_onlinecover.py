"""The white box for medialib/lib/onlinecover.py.

What the audio is taken for, how each site's answer is read, which answers are
taken as the same title, the order the table is asked in, and when a converted
track is worth a search at all. The network never answers here: every case
hands the search its own fetch and download.
"""

import json
import os

import pytest

from medialib.lib import onlinecover
from medialib.lib.onlinecover import Candidate, CoverSearch, Row, Subject

pytestmark = pytest.mark.fs

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 4000
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 4000


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    onlinecover.forget()
    monkeypatch.setenv("coverSearch", "on")
    monkeypatch.setattr(onlinecover, "probe_tags", lambda path: {})
    yield
    onlinecover.forget()


# --- what the audio is -------------------------------------------------------------

@pytest.mark.parametrize("name, minutes, content, why", [
    ("20240105 A Talk About Kites", 5, "podcast", "the name starts with a date"),
    ("20240105_A Talk About Kites", 0, "podcast", "the name starts with a date"),
    ("A Talk About Kites", 45, "podcast", "a length of 45:00"),
    ("A Talk About Kites", 200, "podcast", "a length of 3:20:00"),
    ("The Long Book", 201, "audiobook", "a length of 3:21:00"),
    ("The Long Book", 600, "audiobook", "a length of 10:00:00"),
    ("Short Thing", 12, "other", "a length of 12:00"),
    ("20241399 Not A Date", 5, "other", "a length of 5:00"),
    ("2024010512 Too Many Digits", 5, "other", "a length of 5:00"),
])
def test_probable_content(name, minutes, content, why):
    assert onlinecover.probable_content(name, minutes * 60) == (content, why)


@pytest.mark.parametrize("name, found", [
    ("Kites in Winter Light [aB3_x-9QzK1]", "aB3_x-9QzK1"),
    ("Kites in Winter Light [aB3_x-9QzK1].opus", "aB3_x-9QzK1"),
    ("Kites [Remastered]", ""),
    ("Kites [aB3_x-9QzK1] part 2", ""),
])
def test_youtube_id(name, found):
    assert onlinecover.youtube_id(name) == found


def test_podcast_length_in_stereo_is_a_full_cast_drama():
    assert onlinecover.probable_content(
        "The Lantern Keepers", 90 * 60, stereo=True) == \
        ("audiobook", "a stereo recording of 1:30:00")
    assert onlinecover.probable_content(
        "The Lantern Keepers", 90 * 60) == ("podcast", "a length of 1:30:00")
    # dated or YouTube-named stays a podcast, and short stays other
    assert onlinecover.probable_content(
        "20240105 Lanterns", 90 * 60, stereo=True) == \
        ("podcast", "the name starts with a date")
    assert onlinecover.probable_content(
        "Lanterns [aB3_x-9QzK1]", 90 * 60, stereo=True) == \
        ("podcast", "the name ends in a YouTube id")
    assert onlinecover.probable_content(
        "Lanterns", 10 * 60, stereo=True) == ("other", "a length of 10:00")


def test_a_youtube_id_is_a_podcast_whatever_its_length():
    assert onlinecover.probable_content(
        "Kites [aB3_x-9QzK1]", 5 * 60) == ("podcast",
                                           "the name ends in a YouTube id")
    assert onlinecover.probable_content(
        "Kites [aB3_x-9QzK1]", 600 * 60) == ("podcast",
                                             "the name ends in a YouTube id")


def test_clean_name_drops_the_date_the_brackets_and_underscores():
    assert onlinecover.clean_name("20240105 - The_Quiet Orchard (Unabridged) [B0X1]") \
        == "The Quiet Orchard"


def test_subject_offers_both_halves_of_a_dashed_name():
    subject = onlinecover.subject_for("Ann Mirelle - The Quiet Orchard", 0)
    assert subject.titles == ["Ann Mirelle - The Quiet Orchard",
                              "The Quiet Orchard", "Ann Mirelle"]
    assert subject.creators[:2] == ["Ann Mirelle", "The Quiet Orchard"]
    assert subject.trusted_creators == []


# --- the table ----------------------------------------------------------------------

def test_the_shipped_table_reads_whole():
    rows = onlinecover.read_table(onlinecover.table_path())
    assert rows
    assert {row.content for row in rows} == set(onlinecover.CONTENTS)
    podcast = [row.provider for row in rows if row.content == "podcast"]
    assert podcast[0] == "youtube"
    audiobook = [(row.provider, row.match) for row in rows
                 if row.content == "audiobook"]
    assert ("amazon", "strict") in audiobook
    # every loose row comes after every strict one
    matches = [match for _provider, match in audiobook]
    assert matches.index("loose") > max(i for i, m in enumerate(matches)
                                        if m == "strict")


def test_a_bad_row_is_skipped_and_the_rest_kept(tmp_path):
    table = tmp_path / "t.tsv"
    table.write_text("# c\n"
                     "podcast\tyoutube\tstrict\t1\tytsearch5:{title}\n"
                     "podcast\tnowhere\tstrict\t1\thttps://x\n"
                     "podcast\titunes\tsure\t1\thttps://x\n"
                     "film\titunes\tstrict\t1\thttps://x\n"
                     "podcast\titunes\tstrict\tsoon\thttps://x\n"
                     "podcast\titunes\tstrict\t1\n", encoding="utf-8")
    rows = onlinecover.read_table(str(table))
    assert rows == [Row("podcast", "youtube", "strict", 1.0, "ytsearch5:{title}")]


def test_the_setting_points_at_another_table(monkeypatch, tmp_path):
    monkeypatch.setenv("coverSearchTable", str(tmp_path / "mine.tsv"))
    assert onlinecover.table_path() == str(tmp_path / "mine.tsv")


@pytest.mark.parametrize("value, on", [("on", True), ("off", False),
                                       ("OFF", False), ("0", False), ("1", True)])
def test_the_switch(monkeypatch, value, on):
    monkeypatch.setenv("coverSearch", value)
    assert onlinecover.enabled() is on


# --- the readers --------------------------------------------------------------------

def test_itunes_asks_for_the_large_artwork():
    text = json.dumps({"results": [{
        "collectionName": "Kite Talk", "artistName": "Ann Mirelle",
        "artworkUrl600": "https://is1.example/a/600x600bb.jpg"}]})
    [found] = onlinecover.read_itunes(text)
    assert found.title == "Kite Talk"
    assert found.creators == ("Ann Mirelle",)
    assert found.images == ("https://is1.example/a/1400x1400bb.jpg",
                            "https://is1.example/a/600x600bb.jpg")


def test_audible_reads_authors_runtime_and_the_largest_image():
    text = json.dumps({"products": [{
        "title": "The Quiet Orchard", "authors": [{"name": "Ann Mirelle"}],
        "runtime_length_min": 600,
        "product_images": {"500": "https://m.example/I/a._SL500_.jpg",
                           "1215": "https://m.example/I/a._SL1215_.jpg"}}]})
    [found] = onlinecover.read_audible(text)
    assert found.creators == ("Ann Mirelle",)
    assert found.seconds == 36000
    assert found.images == ("https://m.example/I/a.jpg",
                            "https://m.example/I/a._SL1215_.jpg",
                            "https://m.example/I/a._SL500_.jpg")


def test_amazon_reads_results_and_takes_the_full_size_image():
    page = ('<div data-component-type="s-search-result">'
            '<img class="s-image" src="https://m.example/I/b._AC_UY218_.jpg" alt="x">'
            '<h2><span>The Quiet Orchard</span></h2>'
            '<span>by Ann Mirelle</span></div>')
    [found] = onlinecover.read_amazon(page)
    assert found.title == "The Quiet Orchard"
    assert found.images[0] == "https://m.example/I/b.jpg"
    assert not found.creators_certain
    assert "Ann Mirelle" in found.creators[0]


def test_a_robot_check_reads_as_no_answer():
    assert onlinecover.read_amazon("<html>Enter the characters you see</html>") == []
    assert onlinecover.read_amazon(None) == []


def test_openlibrary_and_youtube():
    [book] = onlinecover.read_openlibrary(json.dumps({"docs": [
        {"title": "The Quiet Orchard", "author_name": ["Ann Mirelle"], "cover_i": 7},
        {"title": "No Cover"}]}))
    assert book.images == ("https://covers.openlibrary.org/b/id/7-L.jpg",)
    [video] = onlinecover.read_youtube(json.dumps({"entries": [
        {"id": "abc", "title": "Kites", "duration": 3000, "channel": "Kite Talk"}]}))
    assert video.seconds == 3000
    assert video.images[0] == "https://i.ytimg.com/vi/abc/maxresdefault.jpg"


def test_a_body_that_is_not_json_reads_as_no_answer():
    for reader in (onlinecover.read_itunes, onlinecover.read_audible,
                   onlinecover.read_openlibrary, onlinecover.read_youtube):
        assert reader("<html>") == []
        assert reader(None) == []


# --- is it the same thing -------------------------------------------------------------

def _subject(titles=("The Quiet Orchard",), creators=("Ann Mirelle",),
             trusted=("Ann Mirelle",), seconds=36000.0, content="audiobook"):
    return Subject(list(titles), list(creators), list(trusted), seconds, content)


def test_strict_takes_the_same_title_by_the_same_author():
    assert onlinecover.accepts("strict", _subject(), Candidate(
        "The Quiet Orchard: A Novel", ("Mirelle, Ann",), 36500))


def test_strict_refuses_another_author():
    assert not onlinecover.accepts("strict", _subject(), Candidate(
        "The Quiet Orchard", ("Bo Treadwell",), 36000))


def test_strict_refuses_another_length():
    assert not onlinecover.accepts("strict", _subject(), Candidate(
        "The Quiet Orchard", ("Ann Mirelle",), 18000))


def test_strict_wants_more_than_a_short_title_alone():
    unknown_length = Subject(["Prologue"], [], [], 0, "other")
    assert not onlinecover.accepts("strict", unknown_length, Candidate("Prologue"))
    known_length = Subject(["Prologue"], [], [], 600, "other")
    assert onlinecover.accepts("strict", known_length,
                               Candidate("Prologue", (), 590))


def test_an_untagged_guess_agrees_but_never_contradicts():
    subject = _subject(creators=("Library",), trusted=())
    assert onlinecover.accepts("strict", subject, Candidate(
        "The Quiet Orchard", ("Bo Treadwell",), 0))


def test_loose_takes_another_edition():
    assert onlinecover.accepts("loose", _subject(), Candidate(
        "The Quiet Orchard", ("Bo Treadwell",), 100))
    assert not onlinecover.accepts("loose", _subject(), Candidate(
        "The Loud Orchard", ("Ann Mirelle",)))


def test_show_matches_the_podcast_by_its_name():
    subject = _subject(titles=("Episode 12",), creators=("Kite Talk",), trusted=())
    assert onlinecover.accepts("show", subject, Candidate("Kite Talk"))
    assert not onlinecover.accepts("show", subject, Candidate("Kite Chat"))


# --- the search ------------------------------------------------------------------------

class _Net:
    def __init__(self, answers, images):
        self.answers, self.images = answers, images
        self.fetched, self.downloaded = [], []

    def fetch(self, url):
        self.fetched.append(url)
        for prefix, text in self.answers.items():
            if url.startswith(prefix):
                return text
        return None

    def download(self, url, dest):
        self.downloaded.append(url)
        data = self.images.get(url)
        if data is None:
            return False
        with open(dest, "wb") as handle:
            handle.write(data)
        return True


ROWS = [
    Row("podcast", "youtube", "strict", 0, "ytsearch5:{title}"),
    Row("podcast", "itunes", "show", 0, "https://it/search?term={creator}"),
    Row("audiobook", "audible", "strict", 0, "https://au/?k={query}"),
    Row("audiobook", "openlibrary", "loose", 0, "https://ol/?q={query}"),
]


def test_the_rows_for_the_content_are_asked_in_order(tmp_path):
    net = _Net({"https://au/": json.dumps({"products": []}),
                "https://ol/": json.dumps({"docs": [
                    {"title": "The Quiet Orchard", "cover_i": 7}]})},
               {"https://covers.openlibrary.org/b/id/7-L.jpg": PNG})
    search = CoverSearch(ROWS, net.fetch, net.download, pace=False)
    image, said = search.find(_subject(), str(tmp_path / "book"))
    assert net.fetched == ["https://au/?k=The+Quiet+Orchard+Ann+Mirelle",
                           "https://ol/?q=The+Quiet+Orchard+Ann+Mirelle"]
    assert image == str(tmp_path / "book.png")
    with open(image, "rb") as handle:
        assert handle.read() == PNG
    assert said.startswith("openlibrary, loose")


def test_the_id_in_the_name_asks_for_that_video_and_takes_it(tmp_path):
    rows = [Row("podcast", "youtube", "id", 0,
                "https://www.youtube.com/watch?v={youtube_id}"), *ROWS]
    net = _Net({"https://www.youtube.com/watch?v=aB3_x-9QzK1": json.dumps(
        {"id": "aB3_x-9QzK1", "title": "Something Else Entirely"})},
        {"https://i.ytimg.com/vi/aB3_x-9QzK1/maxresdefault.jpg": JPEG})
    search = CoverSearch(rows, net.fetch, net.download, pace=False)
    subject = onlinecover.subject_for("Kites [aB3_x-9QzK1]", 300)
    assert subject.titles[0] == "Kites"
    image, said = search.find(subject, str(tmp_path / "ep"))
    assert net.fetched == ["https://www.youtube.com/watch?v=aB3_x-9QzK1"]
    assert image == str(tmp_path / "ep.jpg")
    assert said.startswith("youtube, id match")


def test_without_an_id_the_id_row_is_passed_over(tmp_path):
    rows = [Row("podcast", "youtube", "id", 0,
                "https://www.youtube.com/watch?v={youtube_id}"), *ROWS]
    net = _Net({}, {})
    search = CoverSearch(rows, net.fetch, net.download, pace=False)
    search.find(Subject(["Kites"], [], [], 3000, "podcast"), str(tmp_path / "e"))
    assert net.fetched == ["ytsearch5:Kites"]


def test_a_short_track_named_with_an_id_is_searched(tmp_path):
    track = _touch(tmp_path / "show" / "Kites [aB3_x-9QzK1].opus")
    assert onlinecover.track_wants_search(track, 300, lambda p: False)


def test_an_image_that_will_not_download_falls_to_the_next(tmp_path):
    net = _Net({"ytsearch5:": json.dumps({"entries": [
        {"id": "v1", "title": "Kites in Winter Light", "duration": 3000}]})},
        {"https://i.ytimg.com/vi/v1/hqdefault.jpg": JPEG})
    search = CoverSearch(ROWS, net.fetch, net.download, pace=False)
    subject = Subject(["Kites in Winter Light"], ["Kite Talk"], [], 3010, "podcast")
    image, _said = search.find(subject, str(tmp_path / "ep"))
    assert net.fetched == ["ytsearch5:Kites in Winter Light"]
    assert net.downloaded == ["https://i.ytimg.com/vi/v1/maxresdefault.jpg",
                              "https://i.ytimg.com/vi/v1/hqdefault.jpg"]
    assert image == str(tmp_path / "ep.jpg")


def test_a_row_needing_a_name_the_subject_lacks_is_skipped(tmp_path):
    net = _Net({}, {})
    search = CoverSearch(ROWS, net.fetch, net.download, pace=False)
    subject = Subject(["Kites"], [], [], 3000, "podcast")
    assert search.find(subject, str(tmp_path / "ep")) == ("", "")
    assert net.fetched == ["ytsearch5:Kites"]


def test_the_line_says_what_the_content_was_taken_on(tmp_path, monkeypatch):
    """The content and the figure it was taken on sit in the line's
    parentheses, on the answer that was taken and the one that was not."""
    said = []
    monkeypatch.setattr(onlinecover, "_log", said.append)
    rows = [Row("podcast", "youtube", "strict", 0, "ytsearch5:{title}")]
    net = _Net({"ytsearch5:Kites": json.dumps({"entries": [
        {"id": "v1", "title": "Kites", "duration": 3000,
         "channel": "Kite Talk"}]})},
        {"https://i.ytimg.com/vi/v1/maxresdefault.jpg": JPEG})
    search = CoverSearch(rows, net.fetch, net.download, pace=False)
    subject = onlinecover.subject_for("20240105 Kites", 3000)
    onlinecover._search(subject, str(tmp_path / "ep"), search)
    assert ("    Cover found online (podcast - the name starts with a date): "
            "youtube, strict match: Kites" in said), said

    net = _Net({}, {})
    search = CoverSearch(rows, net.fetch, net.download, pace=False)
    subject = onlinecover.subject_for("A Short Thing", 12 * 60)
    onlinecover._search(subject, str(tmp_path / "no"), search)
    assert ("    No cover found online either (other - a length of 12:00, "
            'looked for "A Short Thing")' in said), said


def test_a_stereo_book_of_podcast_length_is_looked_for_as_an_audiobook(
        tmp_path, monkeypatch):
    """A joined book of podcast length encoded in stereo is a full-cast
    drama, not a talk: the audiobook rows are asked, not the podcast ones."""
    monkeypatch.setenv("coverSearch", "on")
    said = []
    monkeypatch.setattr(onlinecover, "_log", said.append)
    book = tmp_path / "Night Harbour - Episode 12 - The Glass Lighthouse"
    book.mkdir()
    for stereo, content in ((False, "podcast"), (True, "audiobook")):
        net = _Net({}, {})
        rows = [Row(c, "audible", "strict", 0, "%s:{title}" % c)
                for c in ("podcast", "audiobook")]
        search = CoverSearch(rows, net.fetch, net.download, pace=False)
        onlinecover.book_cover(str(book), [], 45 * 60 + 55,
                               str(tmp_path / "b"), search, stereo=stereo)
        assert [a.split(":")[0] for a in net.fetched] == [content]
    assert said[-1].startswith("    No cover found online either (audiobook - "
                               "a stereo recording of 45:55"), said


# --- convert-audio: worth a search? ------------------------------------------------------

def _touch(path, data=b""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return str(path)


def test_a_short_album_track_is_never_searched(tmp_path):
    track = _touch(tmp_path / "album" / "01 Song.flac")
    assert not onlinecover.track_wants_search(track, 240, lambda p: True)


def test_a_folder_image_means_the_album_case(tmp_path):
    track = _touch(tmp_path / "show" / "Long Episode.mp3")
    _touch(tmp_path / "show" / "folder.jpg")
    assert not onlinecover.track_wants_search(track, 3600, lambda p: True)


def test_a_long_track_among_covered_ones_is_searched(tmp_path):
    track = _touch(tmp_path / "show" / "Long Episode.opus")
    covered = {_touch(tmp_path / "show" / "a.opus"), _touch(tmp_path / "show" / "b.opus")}
    _touch(tmp_path / "show" / "c.opus")
    assert onlinecover.track_wants_search(track, 3600, lambda p: p in covered)


def test_a_long_track_among_uncovered_ones_is_not(tmp_path):
    track = _touch(tmp_path / "show" / "Long Episode.opus")
    _touch(tmp_path / "show" / "a.opus")
    _touch(tmp_path / "show" / "b.opus")
    assert not onlinecover.track_wants_search(track, 3600, lambda p: False)


def test_a_lone_long_track_and_a_dated_short_one_are_searched(tmp_path):
    lone = _touch(tmp_path / "book" / "The Quiet Orchard.m4b")
    assert onlinecover.track_wants_search(lone, 30000, lambda p: False)
    dated = _touch(tmp_path / "news" / "20240105 Morning Brief.mp3")
    assert onlinecover.track_wants_search(dated, 600, lambda p: False)


def test_track_cover_fills_the_temp_cover(tmp_path):
    source = _touch(tmp_path / "in" / "Kites in Winter Light.opus")
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    net = _Net({"ytsearch5:": json.dumps({"entries": [
        {"id": "v1", "title": "Kites in Winter Light", "duration": 3000}]})},
        {"https://i.ytimg.com/vi/v1/maxresdefault.jpg": JPEG})
    search = CoverSearch(ROWS, net.fetch, net.download, pace=False)
    onlinecover.track_cover(source, 3000, str(scratch), search, lambda p: False)
    assert (scratch / "tempCover.jpg").read_bytes() == JPEG


def test_track_cover_leaves_a_cover_the_source_gave(tmp_path):
    source = _touch(tmp_path / "in" / "Kites.opus")
    scratch = tmp_path / "tmp"
    _touch(scratch / "tempCover.jpg", b"mine")
    net = _Net({}, {})
    search = CoverSearch(ROWS, net.fetch, net.download, pace=False)
    onlinecover.track_cover(source, 3000, str(scratch), search, lambda p: False)
    assert net.fetched == []
    assert (scratch / "tempCover.jpg").read_bytes() == b"mine"


def test_switched_off_nothing_is_asked(monkeypatch, tmp_path):
    monkeypatch.setenv("coverSearch", "off")
    net = _Net({}, {})
    search = CoverSearch(ROWS, net.fetch, net.download, pace=False)
    assert onlinecover.book_cover(str(tmp_path), [], 36000,
                                  str(tmp_path / "b"), search) == ""
    assert net.fetched == []


def test_image_kind_refuses_a_page_and_a_placeholder(tmp_path):
    assert onlinecover.image_kind(_touch(tmp_path / "a", JPEG)) == "jpg"
    assert onlinecover.image_kind(_touch(tmp_path / "b", PNG)) == "png"
    assert onlinecover.image_kind(_touch(tmp_path / "c", b"<html>" * 1000)) == ""
    assert onlinecover.image_kind(_touch(tmp_path / "d", b"\xff\xd8\xff")) == ""
