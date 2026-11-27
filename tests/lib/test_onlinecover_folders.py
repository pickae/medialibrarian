"""The folders a recording lies in as clues to its cover: every folder between
the one a run was given and the recording, handed to the catalogue search as
possible makers and told the models in full; and an album split into disc
folders looked for by the album's folder rather than "CD1".
"""

import pytest

from medialib.lib import aicontentkind, aicover, onlinecover

pytestmark = pytest.mark.fs


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setenv(aicontentkind.SWITCH_VARIABLE, "off")


def _album(root, *parts, tracks=("01.flac", "02.flac")):
    folder = root.joinpath(*parts)
    folder.mkdir(parents=True)
    for name in tracks:
        (folder / name).write_bytes(b"x")
    return folder, [str(folder / name) for name in tracks]


class TestFoldersBelow:
    def test_every_folder_down_to_its_own(self, tmp_path):
        assert onlinecover.folders_below(
            str(tmp_path), str(tmp_path / "Jazz" / "Artist" / "Album")) \
            == ["Jazz", "Artist", "Album"]

    def test_the_root_itself_is_none_below_it(self, tmp_path):
        assert onlinecover.folders_below(str(tmp_path), str(tmp_path)) == []

    def test_no_root_or_one_elsewhere_says_nothing(self, tmp_path):
        assert onlinecover.folders_below("", str(tmp_path)) is None
        assert onlinecover.folders_below(str(tmp_path / "a"),
                                         str(tmp_path / "b")) is None


@pytest.mark.parametrize("name,disc", [
    ("CD1", True), ("CD 02", True), ("Disc 2", True), ("disk1", True),
    ("cd-3", True), ("CD1 Bonus", False), ("Discovery", False),
    ("Album", False)])
def test_a_disc_folder_is_named_for_its_disc_alone(name, disc):
    assert onlinecover.is_disc_folder(name) is disc


class TestBookSubject:
    def test_a_disc_folder_is_looked_for_as_its_album(self, tmp_path):
        folder, tracks = _album(tmp_path, "Music", "Kites", "Paper Sky", "CD2")
        subject = onlinecover.book_subject(str(folder), tracks, 2400,
                                           probe=lambda _p: {},
                                           root=str(tmp_path / "Music"))
        assert subject.title == "Paper Sky"
        assert "CD2" not in subject.titles
        assert subject.creators[0] == "Kites"
        assert subject.folders == ["Kites", "Paper Sky", "CD2"]

    def test_the_folders_above_its_parent_are_possible_makers(self, tmp_path):
        folder, tracks = _album(tmp_path, "in", "Kites", "Albums", "Paper Sky")
        subject = onlinecover.book_subject(str(folder), tracks, 2400,
                                           probe=lambda _p: {},
                                           root=str(tmp_path / "in"))
        assert subject.creators[:2] == ["Albums", "Kites"]
        # The folder the run was given is no maker.
        assert "in" not in subject.creators

    def test_without_a_root_only_the_parent_is_one(self, tmp_path):
        folder, tracks = _album(tmp_path, "Kites", "Albums", "Paper Sky")
        subject = onlinecover.book_subject(str(folder), tracks, 2400,
                                           probe=lambda _p: {})
        assert "Kites" not in subject.creators
        assert subject.folders is None


def test_a_tracks_folders_are_possible_makers(tmp_path):
    folder, tracks = _album(tmp_path, "in", "Podcasts", "Kite Talk",
                            "Season 2", tracks=("Episode 4.opus",))
    subject = onlinecover.track_subject(tracks[0], 1800, tags={},
                                        root=str(tmp_path / "in"))
    assert subject.creators[:3] == ["Season 2", "Kite Talk", "Podcasts"]
    assert subject.folders == ["Podcasts", "Kite Talk", "Season 2"]


class TestTheModelIsTold:
    def test_the_whole_path_below_the_run_s_folder_for_an_album(
            self, tmp_path):
        folder, tracks = _album(tmp_path, "in", "Jazz", "Kites", "Paper Sky",
                                "CD1")
        subject = onlinecover.book_subject(str(folder), tracks, 2400,
                                           probe=lambda _p: {},
                                           root=str(tmp_path / "in"))
        text = "\n".join(aicover.account(str(folder), "Paper Sky", tracks,
                                         subject, lambda _p: {}))
        assert ("Where it lies below the folder the run was given, outermost "
                "first: Jazz / Kites / Paper Sky / CD1") in text

    def test_the_whole_path_for_a_track(self, tmp_path):
        _folder, tracks = _album(tmp_path, "in", "a", "b", "c", "d", "Show",
                                 tracks=("Episode.opus",))
        subject = onlinecover.track_subject(tracks[0], 1800, tags={},
                                            root=str(tmp_path / "in"))
        text = aicover.track_prompt(tracks[0], subject, lambda _p: {})
        assert "first: a / b / c / d / Show" in text

    def test_the_few_folders_it_sits_in_without_a_root(self, tmp_path):
        _folder, tracks = _album(tmp_path, "Podcasts", "Show",
                                 tracks=("Episode.opus",))
        subject = onlinecover.track_subject(tracks[0], 1800, tags={})
        text = aicover.track_prompt(tracks[0], subject, lambda _p: {})
        assert "The folders it sits in, outermost first: " in text
        assert "Podcasts / Show" in text

    def test_the_content_classifier_too(self, tmp_path):
        folder, tracks = _album(tmp_path, "in", "Hoerspiele", "Nachtschicht")
        text = aicontentkind.prompt(
            "Nachtschicht", folder_path=str(folder), tracks=tracks,
            probe=lambda _p: {}, folders=["Hoerspiele", "Nachtschicht"])
        assert ("Where it lies below the folder the run was given, outermost "
                "first: Hoerspiele / Nachtschicht") in text
        assert "Parent folders" not in text
