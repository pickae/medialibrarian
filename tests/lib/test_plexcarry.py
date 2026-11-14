"""Tests for medialib.lib.plexcarry - what a film takes from its copy in the
Plex library, and what it does not and why.

The profiles are written out here; what makes them is pinned in
tests/lib/test_plexcompare.py."""

import pytest

from medialib.lib import chapterdb
from medialib.lib import plexcarry as pcy
from medialib.lib import plexcompare as pc

pytestmark = pytest.mark.pure

STEADY = pc.Timing(7.9, 1.0)
DRIFTING = pc.Timing(7.9, 1 / 1.001)


def _audio(language="fre", channels=6, name="", track=1, lossless=False,
           ietf=""):
    return pc.Audio(track - 1, language, channels, lossless, False, name,
                    track, ietf, 48000)


def _profile(audio=None, commentaries=(), others=(), chapters=0, named=False,
             edition="", path="new.mkv", width=3840):
    return pc.Profile(path, 5955.0, width, 1600, "SDR",
                      tuple(audio if audio is not None else (_audio(),)),
                      tuple(commentaries), tuple(others), chapters, named,
                      edition)


def _plan(new, old, timing=STEADY, untimed="", keeps=lambda _a: True,
          transcripts=lambda _a: (), chapters=None):
    return pcy.plan(new, old, timing, untimed, keeps, transcripts, chapters)


NAMED = chapterdb.Existing(5942.0, ("Prologue", "Opening credits", "Harbour"),
                           (0.0, 199.2, 288.6))


class TestCommentaryNames:

    def test_a_numbered_commentary_takes_the_librarys_name(self):
        """The case that started it: the same commentary, numbered in the new
        copy and named in the library's."""
        new = _profile(commentaries=[_audio(channels=2, name="Commentary 1",
                                            track=2)])
        old = _profile(path="old.mkv", width=1920, commentaries=[
            _audio(channels=2, name="French commentary with the director",
                   track=2)])
        found = _plan(new, old)
        assert found.names == ((2, "French commentary with the director"),)
        assert found.audio == ()

    def test_names_need_no_sync(self):
        new = _profile(commentaries=[_audio(channels=2, name="Commentary",
                                            track=2)])
        old = _profile(path="old.mkv", commentaries=[
            _audio(channels=2, name="With the director", track=2)])
        found = _plan(new, old, timing=None, untimed="cut differently")
        assert found.names == ((2, "With the director"),)

    @pytest.mark.parametrize("old_names, old_language", [
        (("With the director", "With the cast"), "fre"),
        (("With the director",), "eng")])
    def test_counts_or_languages_that_differ_name_nothing(self, old_names,
                                                          old_language):
        new = _profile(commentaries=[_audio(channels=2, name="Commentary 1",
                                            track=2)])
        old = _profile(path="old.mkv", commentaries=[
            _audio(old_language, 2, name, track=2 + index)
            for index, name in enumerate(old_names)])
        assert _plan(new, old).names == ()

    def test_a_named_commentary_keeps_its_name(self):
        new = _profile(commentaries=[_audio(channels=2, name="Cast commentary",
                                            track=2)])
        old = _profile(path="old.mkv", commentaries=[
            _audio(channels=2, name="Director's commentary", track=2)])
        assert _plan(new, old).names == ()


class TestCarriedAudio:

    def test_commentaries_the_new_copy_lacks_come_with_their_transcripts(self):
        commentary = _audio(channels=2, name="With the director", track=2)
        old = _profile(path="old.mkv", commentaries=[commentary])
        found = _plan(_profile(), old,
                      transcripts=lambda audio: (("old 2 With.en.srt", "en"),))
        assert [(item.kind, item.audio, item.transcripts)
                for item in found.audio] == [
            ("commentary", commentary, (("old 2 With.en.srt", "en"),))]
        assert found.carried == (
            'the commentary "With the director", with its transcripts',)

    def test_lower_resolution_and_a_cut_inside_stays_mixed_and_says_why(self):
        old = _profile(path="old.mkv", width=1920, commentaries=[
            _audio(channels=2, name="With the director", track=2)])
        found = _plan(_profile(), old, timing=None,
                      untimed="the offset changes along the film")
        assert found.audio == ()
        assert found.left == (
            'the commentary "With the director", whisper transcribing it: '
            "the two cannot be put in sync (the offset changes along the "
            "film)",)

    def test_a_lossy_track_at_another_speed_is_left(self):
        old = _profile(path="old.mkv", commentaries=[
            _audio(channels=2, name="With the director", track=2)])
        found = _plan(_profile(), old, timing=DRIFTING)
        assert found.audio == ()
        assert "a lossy track cannot be put to the new one's" in found.left[0]

    def test_a_lossless_track_at_another_speed_is_put_to_it_in_opus(self):
        old = _profile(path="old.mkv", commentaries=[
            _audio(channels=2, name="With the director", track=2,
                   lossless=True)])
        (item,) = _plan(_profile(), old, timing=DRIFTING).audio
        assert item.encode and item.stretch

    def test_numbered_commentaries_against_more_named_ones_are_left(self):
        new = _profile(commentaries=[_audio(channels=2, name="Commentary 1",
                                            track=2)])
        old = _profile(path="old.mkv", commentaries=[
            _audio(channels=2, name="With the director", track=2),
            _audio(channels=2, name="With the cast", track=3)])
        found = _plan(new, old)
        assert found.audio == () and found.names == ()
        assert found.left == ("2 commentaries against the new file's 1, which "
                              "are only numbered: which of them it lacks "
                              "cannot be told",)

    def test_named_commentaries_take_the_ones_they_lack(self):
        new = _profile(commentaries=[_audio(channels=2, name="With the cast",
                                            track=2)])
        old = _profile(path="old.mkv", commentaries=[
            _audio(channels=2, name="With the director", track=2),
            _audio(channels=2, name="With the cast", track=3)])
        (item,) = _plan(new, old).audio
        assert item.audio.name == "With the director"

    def test_a_language_the_new_copy_lacks_and_keeps(self):
        german = _audio("ger", 6, track=2)
        old = _profile(path="old.mkv", audio=[_audio(), german])
        (item,) = _plan(_profile(), old).audio
        assert (item.kind, item.audio) == ("soundtrack", german)

    def test_a_language_the_improved_copy_drops_is_left_and_said(self):
        old = _profile(path="old.mkv", audio=[_audio(), _audio("ger", track=2)])
        found = _plan(_profile(), old,
                      keeps=lambda audio: audio.language == "fre")
        assert found.audio == ()
        assert found.left == ("the ger soundtrack (5.1): not a language the "
                              "improved copy keeps",)

    def test_a_better_soundtrack_in_a_shared_language_is_never_swapped(self):
        old = _profile(path="old.mkv", audio=[_audio(channels=8)])
        found = _plan(_profile(), old)
        assert found.audio == ()
        assert found.left[0].startswith(
            "the better fre soundtrack (7.1 against 5.1)")

    def test_bonus_audio_of_a_kind_the_new_copy_lacks(self):
        score = _audio("zxx", 2, "Isolated Score", track=3)
        old = _profile(path="old.mkv", others=[score])
        (item,) = _plan(_profile(), old).audio
        assert (item.kind, item.audio) == ("other", score)

    def test_no_audio_description_beside_a_soundtrack_in_its_language(self):
        """The improved copy would drop it again."""
        described = _audio("fre", 2, "Audio Description", track=3)
        old = _profile(path="old.mkv", others=[described])
        found = _plan(_profile(), old)
        assert found.audio == ()
        assert found.left == (
            'the audio description track "Audio Description": the improved '
            "copy keeps no audio description beside a soundtrack in its "
            "language",)

    def test_an_audio_description_in_a_language_without_one_is_taken(self):
        described = _audio("ger", 2, "Audio Description", track=3)
        old = _profile(path="old.mkv", others=[described])
        (item,) = _plan(_profile(), old).audio
        assert (item.kind, item.audio) == ("other", described)


class TestChapters:

    def test_named_chapters_are_carried_into_the_new_time(self):
        found = _plan(_profile(chapters=12), _profile(path="old.mkv"),
                      timing=DRIFTING, chapters=NAMED)
        assert found.chapters is not None
        starts = [start for start, _name in found.chapters.chapters]
        assert starts[0] == 0.0
        assert starts[1] == pytest.approx(7.9 + 199.2 / 1.001, abs=0.001)
        assert [name for _start, name in found.chapters.chapters] == list(
            NAMED.names)

    def test_named_chapters_of_its_own_are_kept(self):
        found = _plan(_profile(chapters=12, named=True),
                      _profile(path="old.mkv"), chapters=NAMED)
        assert found.chapters is None

    def test_chapters_need_the_two_in_sync(self):
        found = _plan(_profile(), _profile(path="old.mkv"), timing=None,
                      untimed="no stretch of the two soundtracks matched",
                      chapters=NAMED)
        assert found.chapters is None
        assert found.left == ("the 3 named chapters: the two cannot be put in "
                              "sync (no stretch of the two soundtracks "
                              "matched)",)


def test_a_different_edition_carries_nothing():
    found = _plan(_profile(edition="Extended"), _profile(
        path="old.mkv", commentaries=[_audio(channels=2, name="Commentary")]),
        chapters=NAMED)
    assert not found.anything
    assert found.left == ("nothing: a different edition (editions differ: "
                          "Extended vs no edition), kept beside it",)
