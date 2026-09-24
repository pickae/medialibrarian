"""Tier D for medialib.lib.scenecuts: real picture changes, found and cut on.

tests/lib/test_scenecuts.py settles the choice with frames handed to it. What only
a real decode can answer is that the filters see a hard cut and a black gap where
the picture has them, and that a cut moved onto one still tiles the source: the
chunk after it opens on the new shot, and no frame is encoded twice or lost.
"""

import os
import shutil
import subprocess

import pytest

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_run as run_module
from medialib.lib import scenecuts, segments

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None
                       or shutil.which("ffprobe") is None,
                       reason="tier D needs a real ffmpeg and ffprobe"),
]

# 0-3.6 s one picture, a hard cut to another until 7.2 s, 1 s of black, then
# a third to 11 s. At 25 fps every change lands exactly on a frame.
HARD_CUT = 3.6
BLACK = (7.2, 8.2)
LENGTH = 11.0


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    path = tmp_path_factory.mktemp("scenes") / "in" / "clip.mkv"
    path.parent.mkdir()
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=s=320x240:r=25:d=%s" % HARD_CUT,
         "-f", "lavfi", "-i", "smptebars=s=320x240:r=25:d=%s"
         % (BLACK[0] - HARD_CUT),
         "-f", "lavfi", "-i", "color=black:s=320x240:r=25:d=%s"
         % (BLACK[1] - BLACK[0]),
         "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=%s"
         % (LENGTH - BLACK[1]),
         "-filter_complex", "[0][1][2][3]concat=n=4:v=1,format=yuv420p",
         "-c:v", "libx264", "-g", "250", "-f", "matroska", str(path)],
        check=True, capture_output=True, stdin=subprocess.DEVNULL)
    return path


def _frames(path):
    return int(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "default=nk=1:nw=1",
         str(path)], check=True, capture_output=True, text=True).stdout.strip())


def test_the_hard_cut_scores_as_one(source):
    frames = scenecuts.window_frames(str(source), 2.0, 3.0)
    cuts = [f.time for f in frames if f.score >= scenecuts.SCENE_SCORE]
    assert cuts == [pytest.approx(HARD_CUT, abs=0.001)]


def test_the_black_gap_is_black(source):
    frames = scenecuts.window_frames(str(source), 6.0, 3.0)
    black = [f.time for f in frames if f.black]
    assert black[0] == pytest.approx(BLACK[0], abs=0.001)
    assert black[-1] == pytest.approx(BLACK[1] - 0.04, abs=0.001)


def test_the_cuts_move_onto_the_changes(source):
    bounds = ["0", "3.667", "7.333", "11.000"]
    out, kinds = scenecuts.aligned_bounds(str(source), bounds)
    assert kinds == ["scene", "black"]
    assert float(out[1]) == pytest.approx(HARD_CUT - 0.02, abs=0.001)
    assert BLACK[0] - 0.04 < float(out[2]) < BLACK[1]


@pytest.fixture(scope="module")
def chunked(source, tmp_path_factory):
    patch = pytest.MonkeyPatch()
    patch.setattr(run_module, "log", lambda *a, **k: None)
    root = tmp_path_factory.mktemp("encode")
    settings = rules.Settings(input_dir=str(source.parent),
                              output_dir=str(root / "out"),
                              chunk_root=str(root / "work"),
                              video_profile="x265Fast")
    directory = segments.chunk_dir_for(settings.chunk_root, "clip.mkv")
    os.makedirs(directory, exist_ok=True)
    bounds, _kinds = scenecuts.aligned_bounds(
        str(source), ["0", "3.667", "7.333", "11.000"])
    run_module.Run(settings)._encode(settings, "clip.mkv", directory, LENGTH,
                                     bounds, "")
    yield directory
    patch.undo()


def test_the_chunk_after_the_cut_opens_on_the_new_shot(chunked):
    assert _frames(os.path.join(chunked, "0000.mkv")) == int(HARD_CUT * 25)


def test_the_joined_video_has_exactly_the_sources_frames(chunked, source):
    assert _frames(os.path.join(chunked, "video.mkv")) == _frames(source)
