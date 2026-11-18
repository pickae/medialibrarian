"""convert-audio -r: a run over some folders of the input, whose outputs land
where a run over all of it would put them."""

from __future__ import annotations

import pytest

from medialib.cli import convert_audio

pytestmark = pytest.mark.fs


@pytest.fixture
def tree(tmp_path):
    for folder in ("A", "B/inner", "C"):
        (tmp_path / "in" / folder).mkdir(parents=True)
        (tmp_path / "in" / folder / "one.mp3").write_bytes(b"x")
    return tmp_path


def test_only_the_named_folders_are_read(tree):
    tracks = convert_audio._scan_tracks(str(tree / "in"), ("A", "B"))
    assert sorted(path.replace("\\", "/") for path in tracks) == [
        "A/one.mp3", "B/inner/one.mp3"]


def test_their_paths_are_still_relative_to_the_whole_input(tree):
    """Which is what makes an output's place the same either way."""
    tracks = convert_audio._scan_tracks(str(tree / "in"), ("C",))
    assert [path.replace("\\", "/") for path in tracks] == ["C/one.mp3"]


def test_without_r_everything_is(tree):
    assert len(convert_audio._scan_tracks(str(tree / "in"))) == 3


def test_an_empty_named_folder_is_kept(tree):
    (tree / "in" / "A" / "one.mp3").unlink()
    (tree / "in" / "A" / "deeper").mkdir()
    convert_audio._remove_empty_folders(str(tree / "in"), ("A",))
    assert (tree / "in" / "A").is_dir()
    assert not (tree / "in" / "A" / "deeper").exists()


@pytest.mark.parametrize("folder", ["missing", "../elsewhere"])
def test_a_folder_that_is_not_inside_the_input_is_refused(tree, folder, capsys):
    (tree / "elsewhere").mkdir()
    assert convert_audio.main(["-r", folder, str(tree / "in"), str(tree / "out")]) == 1
    assert not (tree / "out").exists()
