"""Tests for the two lookup tables - medialib.lib.bitrates and .imagesizes.

Each is one table now, so the risk is not drift between copies but a row that
answers where it should not. What is pinned is the answering, and the three
separate ways of having no answer.
"""

import pytest

from medialib.lib import bitrates, imagesizes


class TestTheBitrateTable:
    @pytest.mark.parametrize("channels,column,expected", [
        ("1", "normal", "100"), ("1", "comment", "55"),
        ("2", "normal", "120"), ("2", "comment", "65"),
        ("3", "normal", "150"), ("3", "comment", "85"),
        ("4", "normal", "185"), ("4", "comment", "110"),
        ("5", "normal", "220"), ("5", "comment", "130"),
        ("6", "normal", "250"), ("6", "comment", "150"),
        ("7", "normal", "285"), ("7", "comment", "175"),
        ("8", "normal", "320"), ("8", "comment", "200"),
    ])
    def test_every_row_of_the_table(self, channels, column, expected):
        assert bitrates.audio_bitrate(channels, column) == expected

    @pytest.mark.parametrize("channels", ["9", "12", "0"])
    def test_a_channel_count_with_no_row(self, channels):
        """8 is the ceiling: ffmpeg's libopus wrapper rejects layouts above 7.1,
        so a row beyond it could never encode."""
        assert bitrates.audio_bitrate(channels, "normal") is None

    def test_a_column_that_is_not_a_column(self):
        assert bitrates.audio_bitrate("1", "bogus") is None

    @pytest.mark.parametrize("channels", ["01", "2.0", " 2", "", "1 "])
    def test_the_count_is_matched_as_text_and_not_as_a_number(self, channels):
        """The shell compares the row's first field to the argument as a string, so
        a count that means two but is not spelled "2" finds nothing."""
        assert bitrates.audio_bitrate(channels, "normal") is None

    def test_the_spoken_word_rate_is_always_the_lower_one(self):
        for normal, comment in bitrates._TABLE.values():
            assert int(comment) < int(normal)


class TestTheImageTiers:
    @pytest.mark.parametrize("tier,geometry,height", [
        ("fullHD", "1920x1080", "1080"),
        ("quadHD", "2560x1440", "1440"),
        ("ultraHD4K", "3840x2160", "2160"),
    ])
    def test_every_tier(self, tier, geometry, height):
        assert imagesizes.geometry(tier) == geometry
        assert imagesizes.height(tier) == height

    def test_no_tier_named_is_the_default_one(self):
        assert imagesizes.geometry("") == imagesizes.geometry(imagesizes.DEFAULT_TIER)
        assert imagesizes.height("") == imagesizes.height(imagesizes.DEFAULT_TIER)

    @pytest.mark.parametrize("tier", ["nope", "fullhd", "FullHD", "ultraHD", " fullHD"])
    def test_a_tier_that_is_not_in_the_table_is_an_error(self, tier):
        assert imagesizes.geometry(tier) is None
        assert imagesizes.height(tier) is None

    def test_the_default_is_the_smallest_tier(self):
        assert next(iter(imagesizes._TIERS)) == imagesizes.DEFAULT_TIER

    def test_the_height_is_the_shorter_edge_of_the_geometry(self):
        for tier, (width, tall) in imagesizes._TIERS.items():
            assert int(tall) < int(width)
            assert imagesizes.geometry(tier) == f"{width}x{tall}"
            assert imagesizes.height(tier) == tall
