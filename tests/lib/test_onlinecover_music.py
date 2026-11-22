"""Tests for the music and classical content kinds: telling them apart without
a model, the album catalogues that answer for them, the performer match a
classical release needs, the source folder behind convert-and-concat's
intermediate tree, and the album tags a joined music file is given.
"""

import json
import struct

import pytest

from medialib.lib import (
    aibooktags,
    aicontentkind,
    mutagentags,
    onlinecover,
    runcontext,
)

pytestmark = pytest.mark.pure

_MINI_FLAC = (b"fLaC" + bytes([0x80, 0x00, 0x00, 0x22])
              + struct.pack(">HH", 4092, 4092)
              + b"\x00" * 6
              + struct.pack(">Q", (44100 << 44) | (2 << 41) | (16 << 36))
              + b"\x00" * 16)


@pytest.fixture(autouse=True)
def _no_source_tree():
    runcontext.forget(onlinecover.SOURCE_TREE_VARIABLE)
    yield
    runcontext.forget(onlinecover.SOURCE_TREE_VARIABLE)


class TestMusicContent:
    @pytest.mark.parametrize("genre", ["Hörbuch", "Audiobook", "Spoken Word",
                                       "Hörspiel", "Podcast"])
    def test_a_speech_genre_is_never_music(self, genre):
        assert onlinecover.music_content([{"genre": genre}],
                                         "/lib/Music/Klassik/x") is None

    @pytest.mark.parametrize("genre", ["Classical", "Clásica", "Klassik",
                                       "Baroque", "Opera"])
    def test_a_classical_genre_is_classical(self, genre):
        assert onlinecover.music_content([{"genre": genre}])[0] == "classical"

    def test_another_genre_is_music(self):
        assert onlinecover.music_content([{"genre": "Indie Rock"}]) == (
            "music", "the genre Indie Rock")

    def test_an_empty_or_vague_genre_says_nothing(self):
        assert onlinecover.music_content([{"genre": "Other"}, {"genre": ""}]) \
            is None

    def test_a_conductor_is_classical(self):
        assert onlinecover.music_content([{"conductor": "Ida Sarn"}])[0] \
            == "classical"

    @pytest.mark.parametrize("title", ["Sonata in C, Op. 27 No. 1",
                                       "Suite No. 3, K. 999 - II"])
    def test_a_catalogue_number_is_classical(self, title):
        assert onlinecover.music_content([{"title": title}])[0] == "classical"

    def test_an_untagged_folder_under_klassik_is_classical(self):
        assert onlinecover.music_content(
            [{}], "/media/Audio/Music/Klassik/Velder/Harbour Suite/35 Lindqvist"
        ) == ("classical", "the folder Klassik")

    def test_an_untagged_folder_under_music_is_music(self):
        assert onlinecover.music_content(
            [{}], "/media/Audio/Music/Kites/Paper Sky") == (
                "music", "the folder Music")

    def test_nothing_musical_is_none(self):
        assert onlinecover.music_content(
            [{"album": "The Quiet Orchard"}], "/media/Books/Ann Mirelle") is None

    def test_music_is_read_as_one_word_not_a_substring(self):
        assert onlinecover.music_content([{}], "/media/Musicals Talk") is None


class TestSourceFolder:
    def test_a_folder_of_the_tree_maps_to_its_source(self, tmp_path):
        tree, source = tmp_path / "tree", tmp_path / "Music" / "Kites"
        (tree / "Paper Sky").mkdir(parents=True)
        (source / "Paper Sky").mkdir(parents=True)
        runcontext.settle(onlinecover.SOURCE_TREE_VARIABLE,
                          "%s\t%s" % (tree, source))
        assert onlinecover.source_folder(str(tree / "Paper Sky")) \
            == str(source / "Paper Sky")

    def test_a_folder_without_a_source_stays_itself(self, tmp_path):
        tree, source = tmp_path / "tree", tmp_path / "src"
        (tree / "From An Archive").mkdir(parents=True)
        source.mkdir()
        runcontext.settle(onlinecover.SOURCE_TREE_VARIABLE,
                          "%s\t%s" % (tree, source))
        path = str(tree / "From An Archive")
        assert onlinecover.source_folder(path) == path

    def test_outside_the_tree_or_without_one_nothing_changes(self, tmp_path):
        assert onlinecover.source_folder(str(tmp_path)) == str(tmp_path)
        runcontext.settle(onlinecover.SOURCE_TREE_VARIABLE,
                          "%s\t%s" % (tmp_path / "tree", tmp_path))
        assert onlinecover.source_folder("/elsewhere/x") == "/elsewhere/x"


class TestReaders:
    def test_deezer_albums_largest_cover_first(self):
        text = json.dumps({"data": [
            {"title": "Paper Sky", "artist": {"name": "Kites"},
             "cover_xl": "https://e/xl.jpg", "cover_big": "https://e/big.jpg"},
            {"title": "No Art", "artist": {"name": "Kites"}}]})
        found = onlinecover.read_deezer(text)
        assert [(c.title, c.creators, c.images) for c in found] == [
            ("Paper Sky", ("Kites",), ("https://e/xl.jpg", "https://e/big.jpg"))]

    def test_musicbrainz_groups_ask_the_cover_art_archive(self):
        text = json.dumps({"release-groups": [
            {"id": "abc-123", "title": "Paper Sky",
             "artist-credit": [{"name": "Kites"}]}]})
        found = onlinecover.read_musicbrainz(text)
        assert found[0].creators == ("Kites",)
        assert found[0].images == (
            "https://coverartarchive.org/release-group/abc-123/front",)

    def test_garbage_reads_as_nothing(self):
        assert onlinecover.read_deezer("not json") == []
        assert onlinecover.read_musicbrainz(None) == []


def _classical_subject():
    subject = onlinecover.subject_for("35 Lindqvist (Nordlabel, 1972)", 1800,
                                      stereo=True)
    subject.content = "classical"
    subject.work = "Velder Harbour Suite"
    subject.performer = "Lindqvist"
    subject.composer = "Anton Velder"
    return subject


class TestPerformerMatch:
    def test_the_performers_record_of_the_work_with_couplings(self):
        candidate = onlinecover.Candidate(
            "Velder: Harbour Suite; Ostrand: Night Pieces", ("Elin Lindqvist",))
        assert onlinecover.accepts("performer", _classical_subject(), candidate)

    def test_another_performer_of_the_work_is_not_it(self):
        candidate = onlinecover.Candidate("Velder: Harbour Suite",
                                          ("Tomas Berg",))
        assert not onlinecover.accepts("performer", _classical_subject(),
                                       candidate)

    def test_another_record_of_the_performer_is_not_it(self):
        candidate = onlinecover.Candidate("Ostrand: Night Pieces",
                                          ("Elin Lindqvist",))
        assert not onlinecover.accepts("performer", _classical_subject(),
                                       candidate)

    @pytest.mark.parametrize("folder, label", [
        ("35 Lindqvist (Nordlabel, 1972)", "Nordlabel"),
        ("62 Sarn (Live Label, ~1985)", "Live Label"),
        ("30 Berg (Old Classics, 10.2.1969)", "Old Classics"),
        ("Kites - Paper Sky", ""),
        ("Kites - Paper Sky (Remastered)", ""),
    ])
    def test_a_folder_names_its_label(self, folder, label):
        assert onlinecover._folder_label(folder) == label

    def test_where_the_folder_names_a_label_another_labels_record_is_not_it(self):
        subject = _classical_subject()
        subject.label = "Nordlabel"
        studio = onlinecover.Candidate(
            "Velder: Harbour Suite", ("Elin Lindqvist",),
            label="A Southlabel release, (P) 1971")
        unlabelled = onlinecover.Candidate("Velder: Harbour Suite",
                                           ("Elin Lindqvist",))
        assert not onlinecover.accepts("performer", subject, studio)
        assert not onlinecover.accepts("performer", subject, unlabelled)

    def test_the_labels_own_recital_needs_only_a_word_of_the_work(self):
        subject = _classical_subject()
        subject.label = "Nordlabel"
        recital = onlinecover.Candidate(
            "Ostrand, Velder & Holm: Works for Piano (Live)",
            ("Elin Lindqvist",), label="(P) 2019 Nordlabel")
        assert onlinecover.accepts("performer", subject, recital)

    def test_an_abbreviated_label_reads_as_its_long_name(self):
        assert onlinecover._label_agrees(
            "DG", "(P) 1993 Deutsche Grammophon GmbH, Hamburg")

    def test_the_composer_alone_does_not_count_as_the_performer(self):
        subject = _classical_subject()
        subject.performer = ""
        subject.trusted_creators = ["Anton Velder"]
        candidate = onlinecover.Candidate("Velder: Harbour Suite",
                                          ("Anton Velder",))
        assert not onlinecover.accepts("performer", subject, candidate)


class TestTableAndSearch:
    def test_the_shipped_table_reads_cleanly_with_music_rows(self, monkeypatch):
        said = []
        monkeypatch.setattr(onlinecover, "_log", said.append)
        rows = onlinecover.read_table(onlinecover.table_path())
        assert not said
        contents = {row.content for row in rows}
        assert {"music", "classical"} <= contents
        assert onlinecover.providers_for("classical")[:2] == ["itunes", "deezer"]

    def test_a_classical_row_asks_by_work_and_performer(self, tmp_path):
        row = onlinecover.Row(
            "classical", "itunes", "performer", 0,
            "https://itunes.apple.com/search?term={work}+{performer}&entity=album")
        asked = []

        def fetch(url):
            asked.append(url)
            return json.dumps({"results": [{
                "collectionName": "Velder: Harbour Suite; Ostrand: Night Pieces",
                "artistName": "Elin Lindqvist",
                "artworkUrl100": "https://a/100x100bb.jpg"}]})

        def download(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"\xff\xd8\xff" + b"\0" * 4000)
            return True

        search = onlinecover.CoverSearch([row], fetch=fetch, download=download,
                                         pace=False)
        image, source = search.find(_classical_subject(), str(tmp_path / "c"))
        assert asked == ["https://itunes.apple.com/search?term=Velder+Harbour"
                         "+Suite+Lindqvist&entity=album"]
        assert image.endswith(".jpg") and "performer match" in source


class TestBookSubject:
    def test_an_untagged_performance_under_klassik(self, tmp_path):
        tree = tmp_path / "shm" / "tree"
        source = tmp_path / "Music" / "Klassik" / "Harbour Suite"
        name = "35 Lindqvist (Nordlabel, 1972)"
        (tree / name).mkdir(parents=True)
        (source / name).mkdir(parents=True)
        track = tree / name / "01.opus"
        track.write_bytes(b"x")
        runcontext.settle(onlinecover.SOURCE_TREE_VARIABLE,
                          "%s\t%s" % (tree, source))
        subject = onlinecover.book_subject(str(tree / name), [str(track)],
                                           1800, stereo=True,
                                           probe=lambda path: {})
        assert (subject.content, subject.why) == ("classical",
                                                  "the folder Klassik")
        assert subject.performer == "Lindqvist"
        assert subject.work == "Harbour Suite"

    def test_a_tagged_album_is_music_by_title_and_artist(self, tmp_path):
        folder = tmp_path / "Kites - Paper Sky"
        folder.mkdir()
        track = folder / "01.flac"
        track.write_bytes(b"x")
        tags = {"album": "Paper Sky", "artist": "Kites", "genre": "Indie Rock"}
        subject = onlinecover.book_subject(str(folder), [str(track)], 2400,
                                           stereo=True, probe=lambda p: tags)
        assert subject.content == "music"
        assert (subject.work, subject.performer) == ("Paper Sky", "Kites")

    def test_a_long_stereo_audiobook_stays_what_its_length_says(self, tmp_path):
        folder = tmp_path / "Books" / "The Quiet Orchard"
        folder.mkdir(parents=True)
        track = folder / "01.mp3"
        track.write_bytes(b"x")
        subject = onlinecover.book_subject(
            str(folder), [str(track)], 9 * 3600, stereo=True,
            probe=lambda p: {"genre": "Audiobook"})
        assert subject.content == "audiobook"


class TestClassifier:
    @pytest.mark.parametrize("kind", ["music", "classical"])
    def test_the_model_may_answer_music_and_classical(self, kind):
        verdict = aicontentkind.parse(
            '{"content": "%s", "confidence": "high", "why": "x"}' % kind)
        assert verdict is not None and verdict.content == kind

    def test_the_prompt_offers_both(self):
        assert '"classical"' in aicontentkind.SYSTEM
        assert '"music"' in aicontentkind.SYSTEM


class _Searcher:
    def __init__(self, answer):
        self.answer = answer
        self.name = self.model = "stand-in"
        self.asked = []

    def searching(self, purpose=""):
        return self

    def ask(self, system, prompt, tally=None, images=()):
        self.asked.append((system, prompt))
        return self.answer


class TestMusicTags:
    def test_classical_fields_are_written_apart(self, tmp_path):
        flac = tmp_path / "35 Lindqvist.flac"
        flac.write_bytes(_MINI_FLAC)
        written: list[str] = []
        assert mutagentags.fill_book_tags(str(flac), {
            "composer": "Anton Velder", "work": "Harbour Suite",
            "performer": "Elin Lindqvist", "conductor": "Ida Sarn",
            "ensemble": "Nordic Chamber Orchestra", "label": "Nordlabel",
            "author": "ignored: not a classical field",
        }, written=written, fields=mutagentags.CLASSICAL_TAG_FIELDS) == 0
        assert written == ["composer", "work", "performer", "conductor",
                           "ensemble", "label"]
        tags = mutagentags.read_book_tags(str(flac),
                                          mutagentags.CLASSICAL_TAG_FIELDS)
        assert tags["conductor"] == "Ida Sarn"
        assert tags["ensemble"] == "Nordic Chamber Orchestra"

    def test_tag_book_asks_the_classical_prompt_and_fills_empty_only(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv(aibooktags.SWITCH_VARIABLE, "on")
        flac = tmp_path / "35 Lindqvist.flac"
        flac.write_bytes(_MINI_FLAC)
        assert mutagentags.set_tag(str(flac), "TITLE", "Harbour Suite") == 0
        searcher = _Searcher(json.dumps({
            "title": "Other Title", "composer": "Anton Velder",
            "work": "Harbour Suite", "performer": "Elin Lindqvist",
            "conductor": None, "year": 1972, "label": "Nordlabel",
            "narrator": "not asked for"}))
        subject = _classical_subject()
        written = aibooktags.tag_book(
            str(flac), str(tmp_path), "35 Lindqvist", [], 1800,
            subject=subject, searchers=[searcher], probe=lambda p: {})
        assert searcher.asked[0][0] is aibooktags.CLASSICAL_SYSTEM
        assert "conductor" in searcher.asked[0][1]
        assert written == ["composer", "work", "performer", "year", "label"]
        tags = mutagentags.read_book_tags(str(flac),
                                          mutagentags.CLASSICAL_TAG_FIELDS)
        assert tags["title"] == "Harbour Suite"

    def test_music_answer_is_validated(self):
        tags = aibooktags.parse_music_tags(json.dumps({
            "album": "Paper Sky", "year": "19x2", "genre": "Rock\nPop",
            "artist": "Kites"}))
        assert tags == {"album": "Paper Sky", "artist": "Kites"}
