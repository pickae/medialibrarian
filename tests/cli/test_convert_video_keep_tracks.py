"""convert-video -k: the encode combined with every other track of the source
as it is, by mkvmerge.

What -k promises is that nothing but the picture changes: the audio is not
re-encoded, and each track's name, language and flags, the attachments, the
chapters and the tags come across the way the source has them. The stubbed
half checks what mkvmerge is asked; the media half runs the whole command on a
real file and reads the result back.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_run as run_module
from medialib.lib import toolcapture


@pytest.fixture
def tree(monkeypatch, tmp_path):
    monkeypatch.setattr(run_module, "log", lambda *a, **k: None)
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "film.mkv").write_text("source")
    (tmp_path / "out").mkdir()
    directory = tmp_path / "chunks"
    directory.mkdir()
    (directory / "video.mkv").write_text("video")
    return rules.Settings(input_dir=str(tmp_path / "in"),
                          output_dir=str(tmp_path / "out"), script_dir="",
                          keep_tracks=True, audio_profile="passthrough"), \
        str(directory)


@pytest.mark.fs
class TestTheMux:

    def test_only_the_video_comes_from_the_encode(self, tree, monkeypatch):
        settings, directory = tree
        output = os.path.join(settings.output_dir, "film.mkv")
        asked = []

        def fake_run(argv, *_a, **_k):
            asked.append(argv)
            Path(output).write_text("muxed")
            return toolcapture.ToolRun(argv, 0, toolcapture.ToolOutput())

        monkeypatch.setattr(run_module.toolcapture, "run", fake_run)

        assert run_module.mux_final("film.mkv", directory, settings,
                                    output) == 0
        argv = asked[0]
        assert argv[0] == "mkvmerge"
        video = argv.index(os.path.join(directory, "video.mkv"))
        source = argv.index(os.path.join(settings.input_dir, "film.mkv"))
        for flag in ("--no-audio", "--no-subtitles", "--no-attachments",
                     "--no-chapters", "--no-global-tags"):
            assert argv.index(flag) < video
        assert argv[source - 1] == "--no-video"

    def test_a_warning_from_mkvmerge_is_a_written_file(self, tree,
                                                        monkeypatch):
        settings, directory = tree
        output = os.path.join(settings.output_dir, "film.mkv")

        def warned(argv, *_a, **_k):
            Path(output).write_text("muxed")
            return toolcapture.ToolRun(argv, 1, toolcapture.ToolOutput(),
                                       failure="exited with status 1")

        monkeypatch.setattr(run_module.toolcapture, "run", warned)

        assert run_module.mux_final("film.mkv", directory, settings,
                                    output) == 0

    def test_a_failure_is_one(self, tree, monkeypatch):
        settings, directory = tree
        output = os.path.join(settings.output_dir, "film.mkv")
        monkeypatch.setattr(
            run_module.toolcapture, "run",
            lambda argv, *_a, **_k: toolcapture.ToolRun(
                argv, 2, toolcapture.ToolOutput(),
                failure="exited with status 2"))
        monkeypatch.setattr(run_module.toolcapture, "report_failure",
                            lambda *_a: None)

        assert run_module.mux_final("film.mkv", directory, settings,
                                    output) == 2


@pytest.mark.fs
@pytest.mark.parametrize("audio", [["-a", "opus"], ["-b", "96"]])
def test_an_audio_option_beside_it_is_refused(audio, tmp_path, capsys):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "film.mkv").touch()

    assert run_module.main(["-k"] + audio
                           + [str(tmp_path / "in"), str(tmp_path / "out")]) == 1
    assert "-k keeps the source's audio exactly as it is" in \
        capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def _has_svt():
    if shutil.which("ffmpeg") is None:
        return False
    done = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                          capture_output=True, text=True)
    return " libsvtav1 " in done.stdout


@pytest.mark.media
@pytest.mark.skipif(not _has_svt() or shutil.which("mkvmerge") is None,
                    reason="needs ffmpeg with libsvtav1, and mkvmerge")
class TestARealFile:

    @pytest.fixture(scope="class")
    def converted(self, tmp_path_factory):
        base = tmp_path_factory.mktemp("keep")
        clip = base / "clip.mkv"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=3",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-f", "lavfi", "-i", "sine=frequency=660:duration=3",
             "-map", "0", "-map", "1", "-map", "2",
             "-c:v", "libx264", "-c:a", "flac", str(clip)],
            check=True, capture_output=True, stdin=subprocess.DEVNULL)
        srt = base / "clip.en.srt"
        srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nline 1\n")
        chapters = base / "chapters.txt"
        chapters.write_text("CHAPTER01=00:00:00.000\nCHAPTER01NAME=Opening\n"
                            "CHAPTER02=00:00:01.500\nCHAPTER02NAME=Middle\n")
        source = base / "in" / "film.mkv"
        source.parent.mkdir()
        subprocess.run(
            ["mkvmerge", "-q", "-o", str(source), "--chapters", str(chapters),
             "--language", "1:de", "--track-name", "1:Deutsch FLAC",
             "--language", "2:en", "--track-name", "2:Director's Commentary",
             "--commentary-flag", "2:1", str(clip),
             "--language", "0:fr", "--track-name", "0:Forced",
             "--forced-display-flag", "0:1", str(srt)],
            check=True, capture_output=True)
        out = base / "out"
        env = dict(os.environ)
        done = subprocess.run(
            [sys.executable, "-m", "medialib.cli.convert_video_run", "-k",
             "-p", "av1Fast", "-g", "off", str(source.parent), str(out)],
            capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(rules.__file__)))))
        assert done.returncode == 0, done.stderr
        return source, out / "film.mkv"

    @staticmethod
    def _identify(path):
        return json.loads(subprocess.run(
            ["mkvmerge", "-J", str(path)], check=True, capture_output=True,
            text=True).stdout)

    def test_the_video_was_converted(self, converted):
        _source, output = converted
        tracks = self._identify(output)["tracks"]
        assert tracks[0]["type"] == "video"
        assert tracks[0]["codec"] == "AV1"

    def test_every_other_track_is_the_sources_as_it_was(self, converted):
        source, output = converted

        def others(path):
            return [(track["type"], track["codec"],
                     track["properties"].get("language"),
                     track["properties"].get("track_name"),
                     track["properties"].get("flag_commentary", False),
                     track["properties"].get("forced_track", False))
                    for track in self._identify(path)["tracks"]
                    if track["type"] != "video"]

        assert others(output) == others(source)

    def test_the_chapters_came_across(self, converted):
        source, output = converted
        assert self._identify(output)["chapters"] == \
            self._identify(source)["chapters"]
