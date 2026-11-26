"""Discogs added to a run's copy of the config, and the three beets reports
ingest-music reads after the import."""

import os
import pathlib
import stat
import sys

import pytest

from medialib.lib import beetsextras

pytestmark = pytest.mark.pure

_CONFIG = """directory: .
plugins: fetchart missing

match:
    strong_rec_thresh: 0.10
"""


class TestWithDiscogs:
    def test_the_plugin_is_added_and_the_token_written(self):
        text = beetsextras.with_discogs(_CONFIG, "abc123")
        assert "plugins: fetchart missing discogs\n" in text
        assert text.endswith("discogs:\n    user_token: 'abc123'\n")
        assert "strong_rec_thresh: 0.10" in text

    def test_a_quote_in_the_token_cannot_end_the_string(self):
        text = beetsextras.with_discogs(_CONFIG, "it's")
        assert "user_token: 'it''s'" in text

    def test_a_plugin_already_listed_is_not_listed_twice(self):
        text = beetsextras.with_discogs(
            _CONFIG.replace("missing", "missing discogs"), "t")
        assert "discogs discogs" not in text

    @pytest.mark.parametrize("config", [
        "directory: .\n",
        "plugins: a\nplugins: b\n",
        "plugins: a\ndiscogs:\n    user_token: x\n",
    ], ids=["no plugins", "two plugins lines", "a discogs section"])
    def test_a_config_it_cannot_place_them_in_is_refused(self, config):
        with pytest.raises(ValueError):
            beetsextras.with_discogs(config, "t")

    def test_the_run_copy_is_written_into_the_scratch(self, tmp_path):
        config = tmp_path / "beets.yaml"
        config.write_text(_CONFIG)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        path = beetsextras.write_run_config(str(config), "t", str(scratch))
        assert os.path.dirname(path) == str(scratch)
        assert "discogs" in pathlib.Path(path).read_text(encoding="utf-8")
        assert config.read_text() == _CONFIG
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


class TestReports:
    def test_each_report_is_asked_of_the_right_part_of_the_library(self):
        argvs = beetsextras.report_argvs(["beet", "-c", "c.yaml"], "lib",
                                         "2026-11-26T10:00:00")
        added = "added:2026-11-26T10:00:00.."
        assert argvs["missing"] == ["beet", "-c", "c.yaml", "missing", "-c",
                                    added]
        assert argvs["bad"] == ["beet", "-c", "c.yaml", "bad", added]
        assert argvs["duplicates"] == [
            "beet", "-c", "c.yaml", "duplicates", "-a",
            "path:" + os.path.abspath("lib")]

    def _run(self, outputs, have_flac=True, status=0):
        asked, logged = [], []

        def capture(directory, argv):
            asked.append(argv[3])
            return status, outputs.get(argv[3], "")

        reports = beetsextras.run_reports(
            ["beet", "-c", "c.yaml"], "lib", "2026-11-26T10:00:00",
            logged.append, capture=capture, have_flac=have_flac)
        return reports, asked, logged

    def test_what_each_report_finds_is_logged_and_counted(self):
        reports, asked, logged = self._run({
            "missing": "Artist - Full: 0\nArtist - Short: 2\n",
            "duplicates": "Artist - Album\nArtist - Album\n",
            "bad": "/lib/a.flac: checker exited with status 1\n"
                   "  a.flac: ERROR while decoding\n",
        })
        assert asked == ["missing", "duplicates", "bad"]
        assert reports == beetsextras.Reports(missing=1, duplicates=2, bad=1)
        assert "    Artist - Short: 2" in logged
        assert not any("Full" in line for line in logged)

    def test_without_flac_the_damage_check_is_skipped_and_said(self):
        reports, asked, logged = self._run({}, have_flac=False)
        assert "bad" not in asked
        assert reports.bad == 0
        assert any("no flac tool" in line for line in logged)

    def test_a_report_that_fails_counts_nothing_and_says_so(self):
        reports, _asked, logged = self._run({"missing": "x: 3\n"}, status=1)
        assert reports == beetsextras.Reports()
        assert any("exited with status 1" in line for line in logged)
