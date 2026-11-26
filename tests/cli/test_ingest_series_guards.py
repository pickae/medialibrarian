"""The two guards between the movie and the series ingest: ingest-movies
passes over a show, and ingest-series' refusals. Every name is invented."""

import os

import pytest

from medialib.cli import ingest_movies as movierules
from medialib.cli import ingest_movies_run as movierun
from medialib.cli import ingest_series_run as run
from medialib.lib import treewalk

pytestmark = pytest.mark.fs


def _touch(root, *paths):
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")


class TestIngestMoviesPassesOverAShow:
    def test_a_show_under_the_root_is_held_out(self, tmp_path):
        _touch(tmp_path, "Moss Harbour (2005)/Season 01/Moss Harbour S01E01.mkv",
               "Copper Lantern (1999)/Copper Lantern (1999).mkv")
        roots, names, shows = movierun._without_shows([str(tmp_path)], ["Lib"])
        assert roots == [str(tmp_path)]
        assert [os.path.basename(show) for show in shows] \
            == ["Moss Harbour (2005)"]
        with treewalk.holding_out(shows):
            found = movierules._files_below(
                str(tmp_path), matches=lambda name: name.endswith(".mkv"))
            assert [os.path.basename(path) for path in found] \
                == ["Copper Lantern (1999).mkv"]
            assert [os.path.basename(path)
                    for path in movierun.main_movies(str(tmp_path))] \
                == ["Copper Lantern (1999).mkv"]
        # Held out only for as long as the run holds it.
        assert len(movierules._files_below(str(tmp_path))) == 2

    def test_a_root_that_is_a_show_is_dropped(self, tmp_path):
        _touch(tmp_path, "Season 01/Moss Harbour S01E01.mkv")
        roots, _names, shows = movierun._without_shows([str(tmp_path)], ["Lib"])
        assert roots == []
        assert shows == [str(tmp_path)]


class TestIngestSeriesRefusals:
    def test_split_needs_a_model(self, tmp_path, capsys):
        assert run.main(["-O", "split", str(tmp_path)]) == 1
        assert "only under -A" in capsys.readouterr().err

    def test_write_alone_is_refused(self, tmp_path, capsys):
        assert run.main(["-w", str(tmp_path)]) == 1
        assert "-w on its own" in capsys.readouterr().err

    def test_ids_belong_to_tagging(self, tmp_path, capsys):
        assert run.main(["-O", "layout", "-i", "x.tsv", str(tmp_path)]) == 1
        assert "-i is the id list" in capsys.readouterr().err
