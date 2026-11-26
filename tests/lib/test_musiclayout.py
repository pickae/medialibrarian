"""The music layouts: the names the musicLayout setting takes, the beets config
each ships with, and the beet options a layout that moves albums runs with."""

import os
import re

import pytest

from medialib import commands
from medialib.lib import musiclayout

pytestmark = pytest.mark.pure


class TestChosen:
    def test_unset_is_the_layout_the_download_came_in(self):
        assert musiclayout.chosen("").name == "keep"
        assert not musiclayout.chosen("").beets_names

    @pytest.mark.parametrize("name", ["plex", "jellyfin", "beets"])
    def test_every_other_layout_lets_beets_name_the_library(self, name):
        assert musiclayout.chosen(name).beets_names

    def test_the_name_is_read_without_regard_to_case_or_spaces(self):
        assert musiclayout.chosen(" Plex ").name == "plex"

    def test_a_name_that_is_no_layout_is_refused_with_the_ones_that_are(self):
        with pytest.raises(ValueError) as refused:
            musiclayout.chosen("kodi")
        assert "keep, plex, jellyfin, beets" in str(refused.value)


class TestTheShippedConfigs:
    """Every layout's config ships with the package. Apart from whether beets
    moves the files and where it moves them to, all four say the same, so
    picking a layout does not quietly change the tagging."""

    def _text(self, layout) -> str:
        path = musiclayout.config_path(layout, commands.config_dir())
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    @pytest.mark.parametrize("layout", list(musiclayout.LAYOUTS.values()),
                             ids=list(musiclayout.LAYOUTS))
    def test_each_layout_ships_its_config(self, layout):
        assert os.path.isfile(
            musiclayout.config_path(layout, commands.config_dir()))

    @pytest.mark.parametrize("layout", list(musiclayout.LAYOUTS.values()),
                             ids=list(musiclayout.LAYOUTS))
    def test_the_config_moves_the_files_exactly_when_beets_names_them(
            self, layout):
        moves = re.search(r"^\s+move: (yes|no)\b", self._text(layout), re.M)
        assert moves, layout.name
        assert (moves.group(1) == "yes") == layout.beets_names

    @pytest.mark.parametrize("section", [
        "library", "fetchart", "embedart", "replaygain", "match"])
    def test_the_sections_that_are_not_about_names_are_the_same_in_all(
            self, section):
        def block(text: str) -> str:
            found = re.search(r"^%s:.*?(?=^\S|\Z)" % section, text, re.M | re.S)
            assert found, section
            return found.group(0)
        blocks = {name: block(self._text(layout))
                  for name, layout in musiclayout.LAYOUTS.items()}
        assert len(set(blocks.values())) == 1, blocks

    @pytest.mark.parametrize("layout", list(musiclayout.LAYOUTS.values()),
                             ids=list(musiclayout.LAYOUTS))
    def test_replaygain_runs_on_a_backend_that_reads_flac(self, layout):
        assert re.search(r"^replaygain:\n\s+backend: ffmpeg$",
                         self._text(layout), re.M)

    @pytest.mark.parametrize("layout", list(musiclayout.LAYOUTS.values()),
                             ids=list(musiclayout.LAYOUTS))
    def test_every_config_loads_the_matching_and_report_plugins(self, layout):
        text = self._text(layout)
        plugins = re.search(r"^plugins: (.*)$", text, re.M).group(1).split()
        for plugin in ("fromfilename", "mbsync", "missing", "duplicates",
                       "badfiles"):
            assert plugin in plugins, (layout.name, plugin)
        # Added at run time, with the token only.
        assert "discogs" not in plugins

    @pytest.mark.parametrize("name", ["plex", "jellyfin"])
    def test_the_server_layouts_define_the_fields_their_paths_use(self, name):
        text = self._text(musiclayout.LAYOUTS[name])
        plugins = re.search(r"^plugins: (.*)$", text, re.M).group(1).split()
        assert "inline" in plugins
        for field in ("multidisc", "discnumber"):
            assert "$" + field in text
            assert re.search(r"^\s+%s: " % field, text, re.M), field


class TestTheBeetCommand:
    def test_the_keep_layout_leaves_the_files_where_they_are(self):
        layout = musiclayout.LAYOUTS["keep"]
        assert musiclayout.beet_options(layout, "/srv/music") == []
        assert musiclayout.import_flags(layout) == "-Ciq"
        assert musiclayout.import_flags(layout, retry=True) == "-CIq"

    def test_a_moving_layout_moves_into_the_library_it_names_outright(self):
        layout = musiclayout.LAYOUTS["plex"]
        options = musiclayout.beet_options(layout, "music")
        assert options == ["-d", os.path.abspath("music")]
        assert musiclayout.import_flags(layout) == "-miq"
        assert musiclayout.import_flags(layout, retry=True) == "-mIq"
