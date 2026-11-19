"""Tests for medialib.lib.audiotracks - the role an audio track plays."""

import pytest

from medialib.lib import audiotracks as at
from medialib.lib import enums

pytestmark = pytest.mark.pure


def test_every_role_is_one_of_the_list():
    for name in ("English", "Commentary", "Audio Description", "M&E",
                 "Isolated Score", "Interview"):
        assert at.audio_role(name) in enums.AUDIO_ROLES


def test_a_plain_track_is_the_soundtrack():
    assert at.audio_role("English 5.1") == "main"
    assert at.audio_role("") == "main"
    assert at.audio_role(None) == "main"
    assert at.is_main("Deutsch")


@pytest.mark.parametrize("flag, role", [("commentary", "commentary"),
                                        ("visual_impaired", "description")])
def test_a_flag_says_it(flag, role):
    assert at.audio_role("English", **{flag: True}) == role


@pytest.mark.parametrize("name, role", [
    ("Director's Commentary", "commentary"),
    ("Audiokommentar", "commentary"),
    ("Audio Description", "description"),
    ("Audiodeskription", "description"),
    ("English AD", "description"),
    ("Music & Effects", "effects"),
    ("M&E", "effects"),
    ("Isolated Score", "score"),
    ("Score only", "score"),
    ("Musique", "score"),
    ("Cast Interview", "interview"),
])
def test_a_name_says_it(name, role):
    assert at.audio_role(name) == role


def test_a_lower_case_ad_is_a_word_not_a_description():
    assert at.audio_role("ad libitum") == "main"


def test_the_first_role_that_fits_wins():
    # A commentary flagged for the visually impaired is a commentary.
    assert at.audio_role("English", commentary=True,
                         visual_impaired=True) == "commentary"
    # A commentary on the score is a commentary.
    assert at.audio_role("Commentary over the isolated score") == "commentary"
    # Music and effects is effects before it is music.
    assert at.audio_role("Music & Effects") == "effects"


def test_a_word_that_only_starts_like_music_is_not_a_score():
    assert at.audio_role("English (Musical version)") == "main"
