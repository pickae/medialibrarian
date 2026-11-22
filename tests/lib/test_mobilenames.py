"""The white box for medialib/lib/mobilenames.py."""

import os

import pytest

from medialib.lib import mobilenames as mn

pytestmark = pytest.mark.pure


class TestMobileName:
    @pytest.mark.parametrize("name,expected", [
        ("Act 1: Scene 2.opus", "Act 1 - Scene 2.opus"),
        ("12:30.opus", "12-30.opus"),
        ('Song "Quoted".opus', "Song 'Quoted'.opus"),
        ("Why? Because*.mp3", "Why Because.mp3"),
        ("A | B.m4b", "A - B.m4b"),
        ("<Intro>.flac", "(Intro).flac"),
        ("a\\b.mp3", "a-b.mp3"),
        ("tab\there.mp3", "tabhere.mp3"),
    ])
    def test_what_a_phone_refuses_is_mapped(self, name, expected):
        assert mn.mobile_name(name) == expected

    @pytest.mark.parametrize("name", [
        "CD010 Disc no. 2 (Live Set)",
        "Café – „Grüße“ 東京.opus",
        "01 Allegro con brio.opus",
    ])
    def test_what_a_phone_accepts_is_left_alone(self, name):
        assert mn.mobile_name(name) == name

    def test_leading_and_trailing_dots_and_spaces_go(self):
        assert mn.mobile_name("..hidden ") == "hidden"
        assert mn.mobile_name("Vol.") == "Vol"

    def test_a_name_of_nothing_left_is_still_a_name(self):
        assert mn.mobile_name("???") == "_"

    def test_a_long_name_keeps_its_extension_and_fits(self):
        name = "ö" * 200 + ".opus"
        out = mn.mobile_name(name)
        assert out.endswith(".opus")
        assert len(out.encode("utf-8")) <= mn.MAX_NAME_BYTES

    def test_reserve_keeps_room_for_an_extension_added_later(self):
        out = mn.mobile_name("x" * 300, reserve=4)
        assert len((out + ".m4b").encode("utf-8")) <= mn.MAX_NAME_BYTES

    def test_bytes_that_are_not_utf8_are_read_as_windows_1252(self):
        name = b"Caf\xe9 \x93Song\x94".decode("utf-8", "surrogateescape")
        assert mn.mobile_name(name) == "Café “Song”"


class TestMobilePath:
    def test_every_component_is_mapped(self):
        assert (mn.mobile_path(os.path.join("CD1: Live", "Track?.opus"))
                == os.path.join("CD1 - Live", "Track.opus"))

    def test_an_empty_path_stays_empty(self):
        assert mn.mobile_path("") == ""
