"""Tier D for the chunked video pass: every source frame is encoded exactly once.

The chunks are cut by time and re-joined with a stream copy, so a frame both
sides of a seam claim is encoded twice and the joined video runs long - by one
frame per seam, which puts the sound a frame further out of step at each one and
makes a frame-exact consumer such as HDR10+ reinjection refuse the file.

Which frames a cut selects is ffmpeg's decision, not this code's, so it can
only be pinned against real video. The late-starting source is the case that
first showed it: an AAC track's priming leaves the video starting a few
milliseconds after the container does, and a -t measured from the chunk's first
frame then reaches past the cut. The 25 fps source puts a frame EXACTLY on every
cut, which is where the two ends of a seam must agree on which side it falls.
"""

import os
import shutil
import subprocess

import pytest

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_run as run_module
from medialib.lib import segments

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None
                       or shutil.which("ffprobe") is None,
                       reason="tier D needs a real ffmpeg and ffprobe"),
]


def _source(path, rate, with_audio):
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=%s" % rate]
    if with_audio:
        argv += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                 "-c:a", "aac"]
    argv += ["-t", "3", "-c:v", "libx264", "-g", "12", "-f", "matroska",
             str(path)]
    subprocess.run(argv, check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)
    return path


def _probe(path, entries):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", entries, "-of", "default=nk=1:nw=1", str(path)],
        check=True, capture_output=True, text=True).stdout.strip()


def _frames(path):
    return int(_probe(path, "stream=nb_read_frames"))


SOURCES = {
    # 23.976 fps with an AAC track in front of it: the video starts late.
    "late": ("24000/1001", True),
    # 25 fps from zero: a frame lands exactly on each cut of a 3 s clip.
    "on the cut": ("25", False),
}


@pytest.fixture(scope="module", params=sorted(SOURCES))
def chunked(request, tmp_path_factory, monkeypatch_module):
    rate, with_audio = SOURCES[request.param]
    root = tmp_path_factory.mktemp("chunks")
    source_dir = root / "in"
    source_dir.mkdir()
    source = _source(source_dir / "clip.mkv", rate, with_audio)

    monkeypatch_module.setattr(run_module, "log", lambda *a, **k: None)
    settings = rules.Settings(input_dir=str(source_dir),
                              output_dir=str(root / "out"),
                              chunk_root=str(root / "work"),
                              video_profile="x265Fast")
    directory = segments.chunk_dir_for(settings.chunk_root, "clip.mkv")
    os.makedirs(directory, exist_ok=True)
    duration = run_module._media_duration(str(source))
    run_module.Run(settings)._encode(settings, "clip.mkv", directory, duration,
                                     run_module.chunk_bounds(duration, 3), "")
    return request.param, source, directory


@pytest.fixture(scope="module")
def monkeypatch_module():
    patch = pytest.MonkeyPatch()
    yield patch
    patch.undo()


def test_the_late_source_really_starts_late(chunked):
    name, source, _directory = chunked
    start = float(_probe(source, "stream=start_time"))
    if name == "late":
        assert start > 0
    else:
        assert start == 0


def test_it_was_really_cut_into_three(chunked):
    _name, _source_path, directory = chunked
    for index in range(3):
        assert os.path.getsize(os.path.join(directory, "%04d.mkv" % index)) > 0


def test_the_joined_video_has_exactly_the_sources_frames(chunked):
    _name, source, directory = chunked
    assert _frames(os.path.join(directory, "video.mkv")) == _frames(source)


def test_the_chunks_add_up_to_the_source(chunked):
    """The same claim chunk by chunk, so a failure says which seam."""
    _name, source, directory = chunked
    counts = [_frames(os.path.join(directory, "%04d.mkv" % index))
              for index in range(3)]
    assert sum(counts) == _frames(source), counts
