"""Tests for medialib.lib.commentarynames - naming a film's numbered commentary
tracks after a listing of the disc's extras.

What is pinned here is the refusing. A name put on the wrong track is worse
than the number it replaced, so most of these cases are ones where something is
not quite certain, and the answer they expect is to leave the film alone.
"""

import pytest

from medialib.lib import commentarynames as cn
from medialib.lib.commentarynames import Disc, FileCommentary, Release

pytestmark = pytest.mark.pure

DIRECTOR = "Audio commentary by director Wenna Castellane"
CAST = "Audio commentary with actors Oriel Pask and Tamsin Voller"
CREW = "Audio commentary by cinematographer Ludo Harrowgate"


def _srt(*cues):
    """A transcript: each cue a (seconds, text) pair."""
    lines = []
    for number, (seconds, text) in enumerate(cues, start=1):
        minutes, second = divmod(seconds, 60)
        stamp = "00:%02d:%02d,000" % (minutes, second)
        lines += [str(number), "%s --> %s" % (stamp, stamp), text, ""]
    return "\n".join(lines)


def _tracks(*names):
    return [FileCommentary(str(position + 2), name)
            for position, name in enumerate(names)]


def _bluray(*commentaries, label="Region A"):
    return Release(label, (Disc("bluray", tuple(commentaries)),))


class TestNumberedNames:
    @pytest.mark.parametrize("name, number", [
        ("Commentary", 0),
        ("commentary", 0),
        ("COMMENTARY 2", 2),
        ("Commentary 1", 1),
        ("Audio Commentary #3", 3),
        ("English commentary track 1", 1),
        ("Commentary (English)", 0),
        ("Commentary 2 - English", 2),
        ("Kommentar 2", 2),
        ("Audiokommentar", 0),
        ("Audiokommentar 1", 1),
        ("Commentaire 1", 1),
        ("Commento 2", 2),
    ])
    def test_a_name_that_only_says_commentary_is_numbered(self, name, number):
        assert cn.generic_number(name) == number

    @pytest.mark.parametrize("name, number", [
        ("Audiosommentary 2", 2),
        ("Audiolkommentar", 0),
        ("Audio Comentary 1", 1),
        ("Commmentary 3", 3),
        ("Kommentra 1", 1),
        ("Commentray 2", 2),
    ])
    def test_a_slip_in_the_spelling_is_still_the_word(self, name, number):
        assert cn.generic_number(name) == number

    @pytest.mark.parametrize("name", [
        "Director's Commentary",
        "Commentary with Wenna Castellane",
        "Cast Commentary",
        "Commentary 1 and 2",
        "Commentary 2014",
        "Stereo",
        "Comet 2",
        "",
    ])
    def test_a_name_that_says_more_is_not(self, name):
        assert cn.generic_number(name) is None



class TestTheFilesOrder:
    def test_numbered_tracks_go_by_their_numbers(self):
        tracks = _tracks("Commentary 2", "Commentary 1")
        ordered, why = cn.order_file_commentaries(tracks)
        assert (why, [t.name for t in ordered]) \
            == ("", ["Commentary 1", "Commentary 2"])

    def test_unnumbered_tracks_go_by_where_they_stand(self):
        tracks = _tracks("Commentary", "Audio Commentary")
        assert cn.order_file_commentaries(tracks) == (tracks, "")

    def test_a_named_track_leaves_the_film_alone(self):
        ordered, why = cn.order_file_commentaries(
            _tracks("Commentary 1", "Director's Commentary"))
        assert ordered == [] and "already has a name" in why

    def test_numbers_that_do_not_run_one_to_n_leave_it_alone(self):
        ordered, why = cn.order_file_commentaries(
            _tracks("Commentary 1", "Commentary 3"))
        assert ordered == [] and "not 1 to 2" in why

    def test_the_same_number_twice_leaves_it_alone(self):
        assert cn.order_file_commentaries(
            _tracks("Commentary 1", "Commentary 1"))[0] == []

    def test_some_numbered_and_some_not_leaves_it_alone(self):
        assert cn.order_file_commentaries(
            _tracks("Commentary 1", "Commentary"))[0] == []


class TestTheDisc:
    @pytest.mark.parametrize("width, height, kind", [
        ("3840", "2160", "uhd"),
        ("3840", "1600", "uhd"),
        ("1920", "1080", "bluray"),
        ("1920", "800", "bluray"),
        ("1280", "720", "bluray"),
        ("720", "576", "dvd"),
        ("720", "480", "dvd"),
        ("", "", ""),
    ])
    def test_the_picture_size_says_which_kind(self, width, height, kind):
        assert cn.disc_format(width, height) == kind


class TestWhoIsSpeaking:
    def test_the_capitalised_words_are_the_names(self):
        assert cn.participants(CAST) == {"oriel", "pask", "tamsin", "voller"}

    def test_a_role_written_capitalised_is_not_a_name(self):
        assert cn.participants(
            "Audio Commentary by Director Wenna Castellane and Producer "
            "Ivo Brandt") == {"wenna", "castellane", "ivo", "brandt"}

    def test_the_film_s_own_title_is_nobody(self):
        assert cn.participants("Commentary on Nightfall Harbour by Ivo Brandt",
                               title="Nightfall Harbour") == {"ivo", "brandt"}

    def test_accents_are_folded(self):
        assert cn.participants("Commentary with Zoë Lindqvist") \
            == {"zoe", "lindqvist"}

    def test_an_opening_introduction_is_counted(self):
        mentions = cn.intro_mentions(_srt(
            (5, "Hi, I'm Wenna Castellane, and I directed this film.")))
        assert mentions["castellane"] == 1 and mentions["wenna"] == 1

    def test_a_name_talked_about_is_not_an_introduction(self):
        mentions = cn.intro_mentions(_srt(
            (5, "We found Oriel Pask through an open casting call.")))
        assert mentions["pask"] == 0

    def test_a_name_given_late_in_the_film_is_not_the_opening(self):
        mentions = cn.intro_mentions(_srt(
            (cn.INTRO_SECONDS + 60, "I'm Wenna Castellane")))
        assert mentions["castellane"] == 0


class TestTheTrackTitle:
    def test_audio_commentary_is_shortened(self):
        assert cn.track_title(DIRECTOR) \
            == "Commentary by director Wenna Castellane"

    def test_spacing_and_a_trailing_stop_are_tidied(self):
        assert cn.track_title("  commentary  with Ivo Brandt. ") \
            == "Commentary with Ivo Brandt"

    @pytest.mark.parametrize("note", ["(38:43)", "(4 alternate endings)",
                                      "[new]", "(new) (2019)"])
    def test_a_note_about_the_disc_is_taken_off_the_end(self, note):
        assert cn.track_title("Audio commentary by Ivo Brandt " + note) \
            == "Commentary by Ivo Brandt"

    def test_a_bracket_at_the_end_that_names_people_is_kept(self):
        assert cn.track_title("Audio Commentary by the Cast (Ivo Brandt (Sam) "
                              "and Oriel Pask (Frodo))") \
            == "Commentary by the Cast (Ivo Brandt (Sam) and Oriel Pask (Frodo))"

    def test_a_name_that_no_longer_says_commentary_is_given_the_word(self):
        assert cn.track_title("Wenna Castellane on the making of the film") \
            == "Commentary: Wenna Castellane on the making of the film"


INTRODUCTIONS = {
    DIRECTOR: (10, "Hello, I'm Wenna Castellane, the director."),
    CAST: (20, "Hi, this is Oriel Pask, and I'm here with Tamsin Voller."),
    CREW: (15, "I'm Ludo Harrowgate, I shot the picture."),
}


def _transcripts(tracks, listing):
    """Each track's transcript opening on the introduction of the listed
    commentary it is, in the given order."""
    return {track.id: _srt(INTRODUCTIONS[entry])
            for track, entry in zip(tracks, listing, strict=True)}


class TestTheDecision:
    def test_one_commentary_every_release_agrees_on_is_named(self):
        tracks = _tracks("Commentary")
        decision = cn.decide(tracks, [_bluray(DIRECTOR),
                                      _bluray(DIRECTOR, label="Region B")],
                             "bluray", {})
        assert decision.names == (
            ("2", "Commentary by director Wenna Castellane"),)
        assert decision.source == "Region A, Region B"

    def test_releases_that_word_one_commentary_differently_agree(self):
        tracks = _tracks("Commentary")
        decision = cn.decide(
            tracks, [_bluray(DIRECTOR),
                     _bluray("Commentary with Wenna Castellane",
                             label="Region B")], "bluray", {})
        assert decision.names and decision.reason == ""

    def test_a_count_that_does_not_match_leaves_the_film_alone(self):
        decision = cn.decide(_tracks("Commentary 1", "Commentary 2"),
                             [_bluray(DIRECTOR)], "bluray", {})
        assert decision.names == () and "no Blu-ray release lists that many" \
            in decision.reason

    def test_only_the_kind_of_disc_the_file_came_from_counts(self):
        uhd = Release("4K set", (Disc("uhd", (DIRECTOR,)),
                                 Disc("bluray", (DIRECTOR, CAST))))
        tracks = _tracks("Commentary 1", "Commentary 2")
        transcripts = _transcripts(tracks, (DIRECTOR, CAST))
        assert cn.decide(tracks, [uhd], "bluray", transcripts).names
        assert not cn.decide(tracks, [uhd], "uhd", transcripts).names

    def test_an_unknown_film_leaves_it_alone(self):
        assert cn.decide(_tracks("Commentary"), None, "bluray", {}).names == ()

    def test_an_already_named_film_is_never_looked_at(self):
        decision = cn.decide(_tracks("Director's Commentary"),
                             [_bluray(DIRECTOR)], "bluray", {})
        assert decision.names == () and "already has a name" in decision.reason

    def test_several_are_named_when_the_openings_prove_the_order(self):
        tracks = _tracks("Commentary 1", "Commentary 2", "Commentary 3")
        listing = (DIRECTOR, CAST, CREW)
        decision = cn.decide(tracks, [_bluray(*listing)], "bluray",
                             _transcripts(tracks, listing))
        assert [name for _id, name in decision.names] \
            == [cn.track_title(entry) for entry in listing]

    def test_several_without_transcripts_are_left_alone(self):
        decision = cn.decide(_tracks("Commentary 1", "Commentary 2"),
                             [_bluray(DIRECTOR, CAST)], "bluray", {})
        assert decision.names == () and "no transcript" in decision.reason

    def test_tracks_in_another_order_than_the_menu_go_by_the_openings(self):
        tracks = _tracks("Commentary 1", "Commentary 2")
        decision = cn.decide(tracks, [_bluray(DIRECTOR, CAST)], "bluray",
                             _transcripts(tracks, (CAST, DIRECTOR)))
        assert [name for _id, name in decision.names] \
            == [cn.track_title(CAST), cn.track_title(DIRECTOR)]

    def test_releases_listing_the_same_ones_in_another_order_agree(self):
        tracks = _tracks("Commentary 1", "Commentary 2")
        decision = cn.decide(tracks, [_bluray(DIRECTOR, CAST),
                                      _bluray(CAST, DIRECTOR, label="B")],
                             "bluray", _transcripts(tracks, (DIRECTOR, CAST)))
        assert decision.names and decision.source == "Region A, B"

    def test_openings_that_introduce_nobody_are_left_alone(self):
        tracks = _tracks("Commentary 1", "Commentary 2")
        silent = {track.id: _srt((30, "So this is the opening shot."))
                  for track in tracks}
        assert cn.decide(tracks, [_bluray(DIRECTOR, CAST)], "bluray",
                         silent).names == ()

    def test_one_commentary_naming_nobody_takes_the_track_left_over(self):
        tracks = _tracks("Commentary 1", "Commentary 2")
        transcripts = {tracks[0].id: _srt(INTRODUCTIONS[DIRECTOR]),
                       tracks[1].id: _srt((30, "Welcome, everybody."))}
        decision = cn.decide(tracks, [_bluray(DIRECTOR, "Cast commentary")],
                             "bluray", transcripts)
        assert [name for _id, name in decision.names] \
            == [cn.track_title(DIRECTOR), "Cast commentary"]

    def test_one_whose_people_never_say_who_they_are_takes_the_rest(self):
        tracks = _tracks("Commentary 1", "Commentary 2")
        transcripts = {tracks[0].id: _srt((30, "So we began in the rain.")),
                       tracks[1].id: _srt(INTRODUCTIONS[CREW])}
        decision = cn.decide(tracks, [_bluray(DIRECTOR, CREW)], "bluray",
                             transcripts)
        assert [name for _id, name in decision.names] \
            == [cn.track_title(DIRECTOR), cn.track_title(CREW)]

    def test_two_whose_people_never_say_who_they_are_are_left_alone(self):
        tracks = _tracks("Commentary 1", "Commentary 2", "Commentary 3")
        transcripts = {tracks[0].id: _srt((30, "So we began in the rain.")),
                       tracks[1].id: _srt((30, "This scene took a week.")),
                       tracks[2].id: _srt(INTRODUCTIONS[CREW])}
        assert cn.decide(tracks, [_bluray(DIRECTOR, CAST, CREW)], "bluray",
                         transcripts).names == ()

    def test_disagreeing_releases_are_never_settled_by_elimination(self):
        tracks = _tracks("Commentary 1", "Commentary 2")
        transcripts = {tracks[0].id: _srt(INTRODUCTIONS[DIRECTOR]),
                       tracks[1].id: _srt((30, "So we began in the rain."))}
        assert cn.decide(tracks, [_bluray(DIRECTOR, CAST),
                                  _bluray(DIRECTOR, CREW, label="B")],
                         "bluray", transcripts).names == ()

    def test_the_left_over_track_must_not_introduce_someone_else(self):
        tracks = _tracks("Commentary 1", "Commentary 2")
        transcripts = {tracks[0].id: _srt(INTRODUCTIONS[DIRECTOR]),
                       tracks[1].id: _srt(INTRODUCTIONS[DIRECTOR])}
        assert cn.decide(tracks, [_bluray(DIRECTOR, "Cast commentary")],
                         "bluray", transcripts).names == ()

    def test_two_commentaries_naming_nobody_are_left_alone(self):
        tracks = _tracks("Commentary 1", "Commentary 2")
        transcripts = {track.id: _srt((30, "Welcome.")) for track in tracks}
        assert cn.decide(tracks, [_bluray("Cast commentary",
                                          "Crew commentary")],
                         "bluray", transcripts).names == ()

    def test_someone_on_two_commentaries_does_not_tell_them_apart(self):
        both = "Audio commentary by Wenna Castellane and Ludo Harrowgate"
        tracks = _tracks("Commentary 1", "Commentary 2")
        transcripts = {
            tracks[0].id: _srt(INTRODUCTIONS[DIRECTOR]),
            tracks[1].id: _srt((10, "I'm Wenna, and I'm here with Ludo "
                                    "Harrowgate."))}
        decision = cn.decide(tracks, [_bluray(DIRECTOR, both)], "bluray",
                             transcripts)
        # Wenna is on both, so only Ludo tells the second apart - and the
        # first then names nobody of its own, taking the track left over.
        assert [name for _id, name in decision.names] \
            == [cn.track_title(DIRECTOR), cn.track_title(both)]

    def test_disagreeing_releases_are_settled_by_the_transcripts(self):
        tracks = _tracks("Commentary 1", "Commentary 2")
        transcripts = _transcripts(tracks, (DIRECTOR, CREW))
        decision = cn.decide(tracks, [_bluray(DIRECTOR, CAST),
                                      _bluray(DIRECTOR, CREW,
                                              label="Region B")],
                             "bluray", transcripts)
        assert decision.source == "Region B"

    def test_disagreeing_releases_for_one_commentary_need_a_transcript(self):
        decision = cn.decide(_tracks("Commentary"),
                             [_bluray(DIRECTOR), _bluray(CAST, label="B")],
                             "bluray", {})
        assert decision.names == () and "different commentaries" \
            in decision.reason

    def test_a_transcript_settles_one_commentary_between_releases(self):
        tracks = _tracks("Commentary")
        decision = cn.decide(tracks,
                             [_bluray(DIRECTOR), _bluray(CAST, label="B")],
                             "bluray", _transcripts(tracks, (CAST,)))
        assert decision.names == (("2", cn.track_title(CAST)),)


class _Track:
    """A track the way the ingest reads one, as far as the renames ask."""

    def __init__(self, id, type, name, codec="S_TEXT/UTF8", commentary=True):
        self.id, self.name, self.codec = id, name, codec
        self.is_subtitle = type == "subtitles"
        self.is_commentary = commentary


class TestTheSubtitlesThatFollow:
    def test_a_transcript_takes_its_commentary_s_new_name(self):
        tracks = [_Track("7", "subtitles", "Commentary 1")]
        assert cn.subtitle_renames(tracks, [("Commentary 1", "Commentary by X")]) \
            == ([("7", "Commentary by X")], "")

    def test_the_language_marker_is_kept(self):
        tracks = [_Track("7", "subtitles", "Commentary 2 (DE)"),
                  _Track("8", "subtitles", "Commentary 2 (EN)")]
        renames, _why = cn.subtitle_renames(
            tracks, [("Commentary 2", "Commentary by Y")])
        assert renames == [("7", "Commentary by Y (DE)"),
                           ("8", "Commentary by Y (EN)")]

    def test_the_disc_s_own_subtitle_of_the_commentary_follows_too(self):
        tracks = [_Track("7", "subtitles", "Commentary 1", codec="S_HDMV/PGS")]
        assert cn.subtitle_renames(tracks, [("Commentary 1", "C by X")]) \
            == ([("7", "C by X")], "")

    def test_a_disc_subtitle_numbered_its_own_way_goes_by_the_number(self):
        tracks = [_Track("8", "subtitles", "English (Commentary #2)",
                         codec="S_HDMV/PGS"),
                  _Track("9", "subtitles", "English (Commentary #1)",
                         codec="S_HDMV/PGS")]
        renames, _why = cn.subtitle_renames(
            tracks, [("Commentary 1", "C by X"), ("Commentary 2", "C by Y")])
        assert renames == [("8", "C by Y"), ("9", "C by X")]

    def test_a_single_commentary_s_unnumbered_subtitle_follows(self):
        tracks = [_Track("5", "subtitles", "Commentary", codec="S_VOBSUB")]
        assert cn.subtitle_renames(tracks, [("Commentary", "C by X")]) \
            == ([("5", "C by X")], "")

    def test_a_numbered_transcript_of_no_renamed_commentary_stops_it(self):
        tracks = [_Track("7", "subtitles", "Commentary 3")]
        renames, why = cn.subtitle_renames(
            tracks, [("Commentary 1", "Commentary by X")])
        assert renames == [] and "cannot be told" in why

    def test_a_transcript_two_commentaries_share_a_name_with_stops_it(self):
        tracks = [_Track("7", "subtitles", "Commentary")]
        renames, why = cn.subtitle_renames(
            tracks, [("Commentary", "Commentary by X"),
                     ("Commentary", "Commentary by Y")])
        assert renames == [] and "more than one" in why

    def test_a_named_subtitle_is_left_alone(self):
        tracks = [_Track("7", "subtitles", "Director's Commentary")]
        assert cn.subtitle_renames(tracks, [("Commentary 1", "C by X")]) \
            == ([], "")


class TestTheSidecarsThatFollow:
    STEM = "Nightfall Harbour (1987)"

    def test_every_sidecar_of_the_track_is_renamed(self):
        names = [self.STEM + " 2 Commentary 1.en.srt",
                 self.STEM + " 2 Commentary 1.de.srt",
                 self.STEM + " 2 Commentary 1.opus",
                 self.STEM + " 3 Commentary 2.en.srt",
                 self.STEM + ".en.srt"]
        renames = cn.sidecar_renames(
            names, self.STEM, [("2", "Commentary 1", "Commentary by X")],
            lambda name: name)
        assert sorted(renames) == sorted([
            (self.STEM + " 2 Commentary 1.en.srt",
             self.STEM + " 2 Commentary by X.en.srt"),
            (self.STEM + " 2 Commentary 1.de.srt",
             self.STEM + " 2 Commentary by X.de.srt"),
            (self.STEM + " 2 Commentary 1.opus",
             self.STEM + " 2 Commentary by X.opus")])

    def test_the_name_is_cleaned_the_way_the_transcription_cleans_it(self):
        renames = cn.sidecar_renames(
            [self.STEM + " 2 Commentary 1.en.srt"], self.STEM,
            [("2", "Commentary 1", "Commentary by Dr. X & Y")],
            lambda name: name.replace(".", " ").replace("&", "and"))
        assert renames == [(self.STEM + " 2 Commentary 1.en.srt",
                            self.STEM + " 2 Commentary by Dr  X and Y.en.srt")]

    def test_a_long_name_is_cut_to_fit(self):
        long_name = "Commentary by " + "X" * 300
        [(_old, new)] = cn.sidecar_renames(
            [self.STEM + " 2 Commentary 1.en.srt"], self.STEM,
            [("2", "Commentary 1", long_name)], lambda name: name)
        assert new.endswith(".en.srt") and len(new.encode()) <= 255

    def test_a_sidecar_of_the_track_under_another_name_is_left(self):
        assert cn.sidecar_renames(
            [self.STEM + " 2 Director.en.srt"], self.STEM,
            [("2", "Commentary 1", "Commentary by X")], lambda name: name) \
            == []
