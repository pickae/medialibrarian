"""A movie renamed by the name cleaning, and what goes along with it: its
subtitles, and its commentary transcripts - which the transcription finds by
the film's stem and would otherwise make again."""

from __future__ import annotations

import pytest

from medialib.cli import ingest_movies as im
from medialib.lib import safety

pytestmark = pytest.mark.fs

OLD = "Port d'attache (1999)"
NEW = "Port dattache (1999)"


@pytest.fixture
def film(tmp_path):
    folder = tmp_path / NEW
    folder.mkdir()
    for tail in (".mkv", ".de.srt", " 2 Commentary 1.en.srt",
                 " 2 Commentary 1.fr.srt"):
        (folder / (OLD + tail)).write_text("x")
    return folder


def test_the_commentary_transcripts_follow_the_film(film, tmp_path):
    assert im.rename_movies(str(tmp_path), "", safety.RunSkipLog()) == 1
    assert sorted(path.name for path in film.iterdir()) == [
        NEW + " 2 Commentary 1.en.srt", NEW + " 2 Commentary 1.fr.srt",
        NEW + ".de.srt", NEW + ".mkv"]


def test_a_rename_refused_leaves_the_transcripts_with_the_film(film, tmp_path):
    (film / (NEW + ".mkv")).write_text("another film already there")
    skips = safety.RunSkipLog()
    assert im.rename_movies(str(tmp_path), "", skips) == 0
    assert (film / (OLD + " 2 Commentary 1.en.srt")).is_file()
    assert not (film / (NEW + " 2 Commentary 1.en.srt")).exists()
