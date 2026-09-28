"""The full ingest's research: the chapter archive and the disc database asked
on threads of their own early in the run, and the phases at the end answered
from what they were told - and the -n run's queue, which renames each film
while the database is asked about the next.

Which set or which names a film is given is pinned in tests/lib/test_chapterdb.py
and tests/lib/test_commentarynames.py. What is pinned here is who asks the
sites, and when.
"""

import os
import shutil

import pytest

from medialib.cli import ingest_movies as im
from medialib.cli import ingest_movies_run as run
from medialib.lib import chapterdb, dvdcompare, politepacing, tmdblookup
from medialib.lib.commentarynames import Disc, Release

pytestmark = pytest.mark.fs

FILM = "Nordwind (1998) {imdb-tt0000001}"


def _state(tmp_path):
    return run.Run(script_dir=str(tmp_path / "script"), ram_root="",
                   skips=None, fragments_file="", whisper={},
                   whisper_said=[], ffsubsync_quality="no",
                   long_names=tmdblookup.LongNames(), unfixed_movies=[])


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "Films"
    (root / FILM).mkdir(parents=True)
    (root / FILM / (FILM + ".mkv")).write_bytes(b"")
    monkeypatch.setattr(run, "_has_tool", lambda _name: True)
    monkeypatch.setattr(run, "log", lambda *a, **k: None)
    return root


class TestTheChaptersAreAskedAhead:

    def test_the_phase_asks_the_archive_nothing_new(self, library, tmp_path,
                                                    monkeypatch):
        existing = chapterdb.Existing(8177.0, ())
        monkeypatch.setattr(chapterdb, "existing_chapters",
                            lambda _movie: existing)
        asked = []

        def archive(path, params=()):
            asked.append(path)
            if path == "/browse":
                return ('<td>DVD</td><td><a href="/browse/42">Nordwind</a>'
                        '</td><td>02:16.17</td>')
            return "<chapters/>"

        monkeypatch.setattr(chapterdb, "ask_archive", archive)
        monkeypatch.setattr(run.rules, "_identify", lambda _movie: [])
        ahead = run._start_research(_state(tmp_path), str(library))
        ahead["chapters"].finish(lambda _line: None)
        before = list(asked)
        assert before == ["/browse", "/browse/42.xml"]

        run._chapter_phase(str(library), ahead)
        assert asked == before


class TestTheCommentaryNamesAreAskedAhead:

    def test_the_phase_asks_the_database_nothing_new(self, library, tmp_path,
                                                     monkeypatch):
        tracks = [im.Track(id="0", type="video", codec="V_MPEG4/ISO/AVC",
                           dimensions="1920x1080"),
                  im.Track(id="1", type="audio", codec="A_AC3",
                           name="Commentary")]
        monkeypatch.setattr(run.rules, "_identify", lambda _movie: tracks)
        monkeypatch.setattr(chapterdb, "existing_chapters", lambda _m: None)
        monkeypatch.setattr(dvdcompare, "PACER", politepacing.Pacer(0))
        asked = []
        monkeypatch.setattr(dvdcompare, "_curl",
                            lambda url, form: asked.append(url) or None)
        renamed = []
        monkeypatch.setattr(run, "_carry_out_commentary_names",
                            lambda plan: renamed.append(plan) or "")
        ahead = run._start_research(_state(tmp_path), str(library))
        ahead["names"].finish(lambda _line: None)
        assert len(asked) == 1

        run._commentary_name_phase(_state(tmp_path), str(library), "Films",
                                   ahead)
        assert len(asked) == 1


@pytest.mark.skipif(shutil.which("mkvpropedit") is None,
                    reason="needs mkvpropedit")
class TestTheQueuedNamingRun:

    def test_a_worker_s_failure_comes_back_to_the_list(self, library,
                                                       tmp_path, monkeypatch):
        """The rename is made in a worker process, and what it says comes
        back to the run's list: here mkvpropedit refusing an empty file."""
        tracks = [im.Track(id="0", type="video", codec="V_MPEG4/ISO/AVC",
                           dimensions="1920x1080"),
                  im.Track(id="1", type="audio", codec="A_AC3",
                           name="Commentary")]
        monkeypatch.setattr(run.rules, "_identify", lambda _movie: tracks)
        releases = [Release("Region A", (Disc("bluray", (
            "Audio commentary by director Wenna Castellane",)),))]
        script = tmp_path / "script"
        run._commentary_names_in(str(library), "Films", True,
                                 lambda *_a: releases, "", str(script),
                                 queued=True)
        listing = script / "logs" / "ingest-movies" / \
            "ingest-movies-commentarynames-Films.txt"
        text = listing.read_text(encoding="utf-8")
        assert "# Left alone:" in text
        assert "mkvpropedit could not rename the tracks" in text
        assert os.path.basename(FILM) in text
