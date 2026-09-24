"""Tier D for the HDR10+ round trip: read out of a real source, written back
into a real re-encode that lost it.

What only real video can show is that every frame gets ITS OWN metadata back:
the encode reorders frames (B-frames) and the metadata is stored in decode
order, so a round trip that merely produces HDR10+ could still put each scene's
values on the wrong frames. Each frame of the source is given a distinct value
here, and the output is read back frame by frame, in display order.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_run as run_module

FRAMES = 48

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(any(shutil.which(tool) is None for tool in
                           ("ffmpeg", "ffprobe", "hdr10plus_tool",
                            "mkvmerge")),
                       reason="needs ffmpeg, ffprobe, hdr10plus_tool and "
                              "mkvmerge"),
]


def _ffmpeg(*argv, cwd=None):
    return subprocess.run(["ffmpeg", "-v", "error", "-y", *argv],
                          capture_output=True, text=True, cwd=cwd)


def _average_maxrgb(path):
    """Every frame's HDR10+ average maxRGB, in display order."""
    done = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                           "-show_frames", "-show_entries",
                           "frame=side_data_list", "-of", "compact", path],
                          capture_output=True, text=True)
    return [int(value) for value in
            re.findall(r"average_maxrgb=(\d+)", done.stdout)]


@pytest.fixture
def films(tmp_path):
    """An HDR10+ source whose every frame carries a distinct value, and a
    re-encode of it with no HDR10+ at all - the state convert-video's encode
    leaves the video in."""
    scene = {"BezierCurveData": {"Anchors": [102, 205, 307, 410, 512, 614,
                                             717, 819, 922],
                                 "KneePointX": 150, "KneePointY": 300},
             "NumberOfWindows": 1,
             "TargetedSystemDisplayMaximumLuminance": 400, "SceneId": 0}
    scenes = []
    for index in range(FRAMES):
        frame = dict(scene, SceneFrameIndex=index, SequenceFrameIndex=index)
        frame["LuminanceParameters"] = {
            "AverageRGB": 2000 + index, "MaxScl": [20000, 20000, 20000],
            "LuminanceDistributions": {
                "DistributionIndex": [1, 5, 10, 25, 50, 75, 90, 95, 99],
                "DistributionValues": [0, 10, 50, 200, 800, 2000, 4000, 6000,
                                       9000]}}
        scenes.append(frame)
    metadata = tmp_path / "graded.json"
    metadata.write_text(json.dumps({
        "JSONInfo": {"HDR10plusProfile": "B", "Version": "1.0"},
        "SceneInfo": scenes,
        "SceneInfoSummary": {"SceneFirstFrameIndex": [0],
                             "SceneFrameNumbers": [FRAMES]}}))

    source_dir = tmp_path / "in"
    source_dir.mkdir()
    source = source_dir / "film.mkv"
    made = _ffmpeg("-f", "lavfi", "-i",
                   "testsrc2=s=256x144:r=24000/1001:d=3", "-frames:v",
                   str(FRAMES), "-pix_fmt", "yuv420p10le", "-c:v", "libx265",
                   "-x265-params",
                   "log-level=error:colorprim=bt2020:transfer=smpte2084:"
                   "colormatrix=bt2020nc:dhdr10-info=%s" % metadata.name,
                   str(source),
                   # By name, from its own directory: a Windows path's drive
                   # colon would end the x265 parameter it is written in.
                   cwd=str(tmp_path))
    if made.returncode != 0 or not _average_maxrgb(str(source)):
        pytest.skip("this ffmpeg's x265 cannot write HDR10+, so there is no "
                    "source to test with")

    chunks = tmp_path / "chunks"
    chunks.mkdir()
    # Eight B-frames, so decode order and display order really differ.
    _ffmpeg("-i", str(source), "-c:v", "libx265", "-x265-params",
            "log-level=error:bframes=8", "-pix_fmt", "yuv420p10le",
            str(chunks / "video.mkv"))
    return tmp_path, source_dir, chunks


def test_every_frame_gets_its_own_metadata_back(films, monkeypatch):
    tmp_path, source_dir, chunks = films
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(run_module.ramscratch, "ram_scratch_dir_for",
                        lambda *a: (str(scratch), 0, 0))
    monkeypatch.setattr(run_module, "log", lambda *a, **k: None)
    assert _average_maxrgb(str(chunks / "video.mkv")) == []

    settings = rules.Settings(encoder="libx265", input_dir=str(source_dir),
                              output_dir=str(tmp_path / "out"))
    metadata = run_module.prepare_hdr10plus(
        "film.mkv", str(source_dir / "film.mkv"), str(chunks), settings,
        reframed=False)
    assert metadata
    assert run_module.restore_hdr10plus("film.mkv", str(chunks), metadata,
                                        settings)

    expected = [2000 + index for index in range(FRAMES)]
    assert _average_maxrgb(str(chunks / "video.mkv")) == expected
    # And the final mux, a stream copy, carries it through unchanged.
    final = tmp_path / "final.mkv"
    _ffmpeg("-i", str(chunks / "video.mkv"), "-c", "copy", str(final))
    assert _average_maxrgb(str(final)) == expected
    assert os.listdir(scratch) == []


def test_an_encode_that_lost_frames_is_refused(films, monkeypatch):
    """hdr10plus_tool would stretch the metadata to fit and exit 0; the
    encode keeps plain HDR10 instead."""
    tmp_path, source_dir, chunks = films
    _ffmpeg("-i", str(source_dir / "film.mkv"), "-frames:v",
            str(FRAMES // 2), "-c:v", "libx265", "-x265-params",
            "log-level=error", "-pix_fmt", "yuv420p10le",
            str(chunks / "video.mkv"))
    before = (chunks / "video.mkv").read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(run_module.ramscratch, "ram_scratch_dir_for",
                        lambda *a: (str(scratch), 0, 0))
    said = []
    monkeypatch.setattr(run_module, "log", said.append)

    settings = rules.Settings(encoder="libx265", input_dir=str(source_dir),
                              output_dir=str(tmp_path / "out"))
    metadata = run_module.prepare_hdr10plus(
        "film.mkv", str(source_dir / "film.mkv"), str(chunks), settings,
        reframed=False)
    assert not run_module.restore_hdr10plus("film.mkv", str(chunks), metadata,
                                            settings)
    assert (chunks / "video.mkv").read_bytes() == before
    assert any("the encode has %d frames" % (FRAMES // 2) in line
               for line in said)
