"""Writing the finished encode out, when the place it was going to has changed.

The video encode is hours of work held in RAM until the mux writes it to disk, and
the two things it depends on out there - the output sub-folder and the output name
- both belong to the user, who can delete or fill either while the encode is
running. Neither may cost the encode: a folder that has gone is put back, and a
name that has been taken is stepped around.
"""

import os
from pathlib import Path

import pytest

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_run as run_module
from medialib.lib import dolbyvision

pytestmark = pytest.mark.fs


@pytest.fixture
def tree(monkeypatch, tmp_path):
    """An input file, and an output tree with the mirrored sub-folder in place."""
    monkeypatch.setattr(run_module, "log", lambda *a, **k: None)
    source = tmp_path / "in" / "Films"
    source.mkdir(parents=True)
    (source / "film.mkv").write_text("source")
    (tmp_path / "out" / "Films").mkdir(parents=True)
    return rules.Settings(input_dir=str(tmp_path / "in"),
                          output_dir=str(tmp_path / "out"), script_dir="")


def test_a_deleted_output_folder_is_put_back_to_write_into(tree):
    os.rmdir(os.path.join(tree.output_dir, "Films"))

    chosen = run_module.output_path_for("Films/film.mkv", tree, False)

    assert chosen == os.path.join(tree.output_dir, "Films", "film.mkv")
    assert os.path.isdir(os.path.dirname(chosen))


def test_a_name_taken_during_the_encode_gets_a_sibling(tree):
    taken = os.path.join(tree.output_dir, "Films", "film.mkv")
    Path(taken).write_text("not ours")

    chosen = run_module.output_path_for("Films/film.mkv", tree, False)

    assert chosen == os.path.join(tree.output_dir, "Films", "film (2).mkv")
    assert Path(taken).read_text() == "not ours"


def test_the_out_of_date_output_this_run_replaces_is_written_over(tree):
    stale = os.path.join(tree.output_dir, "Films", "film.mkv")
    Path(stale).write_text("half a film from last time")

    assert run_module.output_path_for("Films/film.mkv", tree, True) == stale


def test_a_mux_that_failed_on_a_missing_folder_is_retried_once(tree, tmp_path,
                                                               monkeypatch):
    output = os.path.join(tree.output_dir, "Films", "film.mkv")
    os.rmdir(os.path.dirname(output))
    attempts = []

    class Result:
        def __init__(self, code):
            self.returncode = code

    def fake_run(argv, capture=False):
        attempts.append(os.path.isdir(os.path.dirname(output)))
        # The first attempt fails the way ffmpeg does with no folder to open;
        # the second finds the folder back and writes.
        if len(attempts) == 1:
            return Result(1)
        Path(output).write_text("muxed")
        return Result(0)

    monkeypatch.setattr(run_module, "_run", fake_run)
    monkeypatch.setattr(rules, "profile_args", lambda *a, **k: "-c:a copy")
    directory = tmp_path / "chunks"
    directory.mkdir()
    (directory / "video.mkv").write_text("video")

    status = run_module.mux_final("Films/film.mkv", str(directory), tree,
                                  output)

    assert status == 0
    assert attempts == [False, True]
    assert Path(output).read_text() == "muxed"


def test_the_video_only_failsafe_puts_the_folder_back_to_keep_the_encode(tree,
                                                                        tmp_path):
    os.rmdir(os.path.join(tree.output_dir, "Films"))
    directory = tmp_path / "chunks"
    directory.mkdir()
    (directory / "video.mkv").write_text("the expensive part")

    kept = run_module.write_video_only("Films/film.mkv", str(directory),
                                       "audio encoding failed", tree)

    assert kept
    written = os.path.join(tree.output_dir, "Films", "film (video only).mkv")
    assert Path(written).read_text() == "the expensive part"


def test_finish_muxes_to_the_sibling_when_the_name_was_taken(tree, tmp_path,
                                                             monkeypatch):
    taken = os.path.join(tree.output_dir, "Films", "film.mkv")
    Path(taken).write_text("not ours")
    directory = tmp_path / "chunks"
    directory.mkdir()
    (directory / "video.mkv").write_text("video")
    monkeypatch.setattr(rules, "video_intermediate_complete",
                        lambda *a, **k: True)
    muxed = []

    def fake_mux(relative, chunk_dir, settings, output):
        muxed.append(output)
        Path(output).write_text("muxed")
        return 0

    monkeypatch.setattr(run_module, "mux_final", fake_mux)
    monkeypatch.setattr(dolbyvision, "normalise_config_level",
                        lambda *a, **k: None)

    state = run_module.Run(tree)
    status = state.finish("Films/film.mkv", str(directory), 600.0, 0)

    assert status == 0
    assert muxed == [os.path.join(tree.output_dir, "Films", "film (2).mkv")]
    assert Path(taken).read_text() == "not ours"
