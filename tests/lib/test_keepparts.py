"""Tests for medialib.lib.keepparts - the named parts an option's value
carries: the -k parts a run is told to keep, the -o phases it is told to
omit."""

import pytest

from medialib.lib import clioptions, keepparts

pytestmark = pytest.mark.pure

PARTS = ("audio", "tracks", "dv")


def test_the_bare_flag_keeps_everything():
    assert keepparts.parse("", PARTS) == frozenset(PARTS)


def test_a_named_part_keeps_only_that():
    assert keepparts.parse("audio", PARTS) == frozenset(("audio",))


def test_named_parts_keep_themselves_and_only_themselves():
    assert keepparts.parse("dv,audio", PARTS) == frozenset(("dv", "audio"))
    assert keepparts.parse("audio,dv", PARTS) == frozenset(("dv", "audio"))


def test_a_repeated_part_is_kept_once():
    assert keepparts.parse("audio,audio", PARTS) == frozenset(("audio",))


def test_a_part_not_allowed_is_refused_naming_the_ones_that_are():
    with pytest.raises(clioptions.UsageError,
                       match=r'one of: audio, tracks, dv \(got "subtitles"\)'):
        keepparts.parse("subtitles", PARTS)


def test_one_bad_part_among_good_ones_is_refused():
    with pytest.raises(clioptions.UsageError, match=r'got "opus"'):
        keepparts.parse("audio,opus", PARTS)


def test_an_empty_part_is_refused_too():
    with pytest.raises(clioptions.UsageError, match=r'got ""'):
        keepparts.parse("audio,,dv", PARTS)
    with pytest.raises(clioptions.UsageError):
        keepparts.parse(",", PARTS)


def test_nothing_allowed_and_nothing_named_keeps_nothing():
    assert keepparts.parse("", ()) == frozenset()


def test_nothing_allowed_and_a_part_named_is_refused():
    with pytest.raises(clioptions.UsageError):
        keepparts.parse("audio", ())


def test_the_result_is_a_set_and_comes_back_equal():
    kept = keepparts.parse("tracks", PARTS)
    assert type(kept) is frozenset
    assert "tracks" in kept and "audio" not in kept


PHASES = ("junk", "subtitles", "commentary", "chapters", "plex", "tagging")


def test_the_refusal_says_the_flag_and_the_word_it_is_given():
    """-k says "-k part", and -o says "-o phase": the refusal is the
    option's own, not the helper's."""
    with pytest.raises(clioptions.UsageError, match=r'^The -k part '):
        keepparts.parse("bogus", PARTS)
    with pytest.raises(clioptions.UsageError,
                       match=r'^The -o phase must be one of: '
                             r'junk, subtitles, commentary, chapters, '
                             r'plex, tagging \(got "subs"\)'):
        keepparts.parse("subs", PHASES, flag="-o", word="phase")


def test_the_bare_flag_names_everything_whatever_the_flag_is():
    assert keepparts.parse("", PHASES, flag="-o", word="phase") \
        == frozenset(PHASES)
