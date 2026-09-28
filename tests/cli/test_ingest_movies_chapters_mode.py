"""The chapters-only run (-c): the chapter lookup, and nothing else.

What is pinned here is the boundary: the lookup runs per folder in the order
given, writes as it goes with no -w to ask for it, and is refused beside every
other phase of its own - and that a set the lookup finds is written by a
worker of the queue the lookup feeds. Which set a film is given is pinned in
tests/lib/test_chapterdb.py.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from medialib.cli import ingest_movies_run as run
from medialib.lib import chapterdb

pytestmark = pytest.mark.fs


def _library(root, name: str, film: str = "The Movie (1999) {imdb-tt0000001}"):
    folder = root / name / film
    folder.mkdir(parents=True)
    (folder / (film + ".mkv")).touch()
    return root / name


def _never(*_args, **_kwargs):
    raise AssertionError("a phase of the full run ran that -c refuses")


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    tools: list = []
    monkeypatch.setattr(run.tooldeps, "require_tools",
                        lambda _program, specs: tools.append(specs) or 0)
    logs: list = []
    monkeypatch.setattr(run, "log", logs.append)
    monkeypatch.setattr(run.tmdblookup, "tag_plex_ids", _never)
    monkeypatch.setattr(run.ramscratch, "ram_scratch_dir", _never)
    for name in ("_ingest", "_subtitles_only", "_tags_only",
                 "_commentary_only"):
        monkeypatch.setattr(run, name, _never)
    calls: list = []

    def findings(directory, log):
        calls.append(directory)
        yield directory + "/film.mkv", "kept", None, None

    monkeypatch.setattr(run.chapterdb, "findings", findings)
    return {"calls": calls, "tools": tools, "logs": logs,
            "script": str(tmp_path / "script")}


def _main(stubbed, *argv):
    return run.main(list(argv), script_dir=stubbed["script"])


class TestThePhaseRunsAndNothingElse:

    def test_each_folder_is_looked_up_in_order(self, stubbed, tmp_path):
        films = _library(tmp_path, "Films")
        docs = _library(tmp_path, "Documentaries")
        assert _main(stubbed, "-c", str(films), str(docs)) == 0
        assert stubbed["calls"] == [str(films), str(docs)]
        assert stubbed["tools"] == [["ffprobe", "curl", "mkvpropedit"]]
        assert any("2 film(s) already named" in line
                   for line in stubbed["logs"])


class TestRefusedBesideAnotherPhase:

    @pytest.mark.parametrize("other", ["-t", "-s", "-a"])
    def test_another_phase_of_its_own_is_refused(self, stubbed, tmp_path,
                                                 capsys, other):
        assert _main(stubbed, "-c", other, str(_library(tmp_path, "F"))) == 1
        assert "-c looks up chapters and nothing else" in \
            capsys.readouterr().err
        assert stubbed["calls"] == []

    def test_w_has_no_part_in_it(self, stubbed, tmp_path, capsys):
        """Not a dry run, so there is nothing for -w to carry out."""
        assert _main(stubbed, "-cw", str(_library(tmp_path, "F"))) == 1
        assert "-w has no part in -c" in capsys.readouterr().err
        assert stubbed["calls"] == []


@pytest.mark.media
@pytest.mark.skipif(any(shutil.which(tool) is None for tool in
                        ("ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit")),
                    reason="needs ffmpeg, ffprobe and mkvtoolnix")
class TestTheQueueWritesWhatTheLookupFound:

    def test_a_found_set_is_written_by_a_worker(self, tmp_path, monkeypatch):
        folder = tmp_path / "Nordwind (1998) {imdb-tt0000001}"
        folder.mkdir()
        raw = tmp_path / "raw.mkv"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "anoisesrc=d=20:c=pink:r=48000", "-c:a", "libopus",
             "-b:a", "32k", str(raw)], check=True, stdin=subprocess.DEVNULL)
        movie = folder / (folder.name + ".mkv")
        subprocess.run(["mkvmerge", "--quiet", "-o", str(movie), str(raw)],
                       check=True, stdin=subprocess.DEVNULL)
        existing = chapterdb.existing_chapters(str(movie))
        chosen = chapterdb.ChapterSet(
            "1", "Nordwind", "Blu-Ray", "eng", 5, 20,
            ((0.0, "Opening"), (5.0, "Harbour"), (12.5, "Storm")))

        def findings(directory, log):
            yield str(movie), "found", existing, chosen
            yield str(folder / "other.mkv"), "unmatched", existing, None

        monkeypatch.setattr(run.chapterdb, "findings", findings)
        monkeypatch.setattr(run, "log", lambda _line: None)
        outcome = run._queued_chapters(str(tmp_path))
        assert outcome["added"] == [str(movie)]
        assert outcome["unmatched"] == [str(folder / "other.mkv")]
        assert chapterdb.existing_chapters(str(movie)).names == (
            "Opening", "Harbour", "Storm")
