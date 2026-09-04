"""The white box for medialib/cli/ingest_movies.py.

The naming and classification rules. `is_commentary_name` and `normalize_title`
are library functions with white boxes of their own, so what is here is the five
rules this script owns.
"""

import pytest

from medialib.cli import ingest_movies as im

pytestmark = pytest.mark.pure


class TestRename:
    """The movie-specific rules layered on the shared cleaner: dots and
    underscores to spaces, an empty trailing bracket pair dropped, whitespace
    collapsed, and a trailing four-digit year wrapped in parentheses."""

    @pytest.mark.parametrize("raw", ["Some.Movie.Title", "Some_Movie_Title",
                                     "Some.Movie_Title"])
    def test_dots_and_underscores_become_spaces(self, raw):
        assert im.rename(raw) == "Some Movie Title"

    def test_whitespace_runs_collapse(self):
        assert im.rename("Some    Movie") == "Some Movie"

    def test_leading_and_trailing_whitespace_is_trimmed(self):
        assert im.rename("   Some Movie   ") == "Some Movie"

    def test_an_apostrophe_does_not_break_the_collapse(self):
        """An apostrophe broke the shell's original `echo | xargs` collapse,
        which treats it as a quote and errors out. The apostrophe itself is
        dropped earlier by the shared cleaner's punctuation pass; what matters
        here is that the name comes through collapsed rather than the whole
        rename failing."""
        assert im.rename("Dante's   Peak") == "Dantes Peak"

    @pytest.mark.parametrize("raw", ["Some Movie ()", "Some Movie ( )"])
    def test_an_empty_trailing_bracket_pair_is_dropped(self, raw):
        """A film whose year was stripped by an earlier pass is otherwise left
        with a bare "()" hanging off it."""
        assert im.rename(raw) == "Some Movie"

    @pytest.mark.parametrize("raw,expected", [
        ("Some Movie 1999", "Some Movie (1999)"),
        ("Some.Movie.2018", "Some Movie (2018)"),
    ])
    def test_a_trailing_year_is_parenthesised(self, raw, expected):
        assert im.rename(raw) == expected

    def test_an_already_parenthesised_year_is_left_alone(self):
        """The pattern is four digits, so the last four characters of an
        already-wrapped year are "999)" and match nothing."""
        assert im.rename("Some Movie (1999)") == "Some Movie (1999)"

    def test_a_trailing_non_year_number_is_not_parenthesised(self):
        assert im.rename("Movie 3001") == "Movie 3001"

    def test_a_leading_find_prefix_survives_the_dots_pass(self):
        """The rename phases feed paths straight from `find .`, so the leading
        "./" must survive becoming "  "."""
        assert im.rename("./Some.Movie.1999") == "./Some Movie (1999)"

    def test_it_is_idempotent_on_its_own_output(self):
        """The rename phases repeat until the names stabilise, so a name that is
        already clean has to come back unchanged."""
        once = im.rename("Some.Movie.1999")
        assert im.rename(once) == once


class TestAudioStreamIndex:
    """mkvmerge's single cross-type track number as the 0-based per-type index
    ffmpeg's "-map 0:a:N" wants: how many audio tracks precede it."""

    @pytest.mark.parametrize("track,types,expected", [
        (2, ["video", "audio"], 0),
        (3, ["video", "audio", "audio"], 1),
        (4, ["video", "audio", "audio", "audio"], 2),
    ])
    def test_the_ordinary_layout(self, track, types, expected):
        assert im.audio_stream_index(track, types) == expected

    @pytest.mark.parametrize("track,types,expected", [
        (4, ["video", "audio", "subtitles", "audio"], 1),
        (4, ["video", "subtitles", "subtitles", "audio"], 0),
    ])
    def test_subtitles_between_the_audio_tracks_do_not_count(self, track,
                                                             types, expected):
        assert im.audio_stream_index(track, types) == expected

    @pytest.mark.parametrize("track,expected", [(1, 0), (2, 1)])
    def test_a_file_with_no_video_at_all(self, track, expected):
        assert im.audio_stream_index(track, ["audio", "audio"]) == expected

    def test_the_type_match_is_a_substring_match(self):
        """mkvmerge's type strings are the spelled-out plurals."""
        assert im.audio_stream_index(3, ["video", "audio", "subtitles"]) == 1


class TestIsImageSubCodec:
    """The three bitmap subtitle codecs, as mkvmerge spells them."""

    @pytest.mark.parametrize("codec", ["S_HDMV/PGS", "S_VOBSUB", "S_DVBSUB"])
    def test_a_bitmap_codec_is_one(self, codec):
        assert im.is_image_sub_codec(codec) is True

    def test_the_match_is_case_insensitive(self):
        assert im.is_image_sub_codec("s_hdmv/pgs") is True

    @pytest.mark.parametrize("codec", ["S_TEXT/UTF8", "S_TEXT/ASS",
                                       "S_TEXT/SSA", "S_TEXT/WEBVTT", ""])
    def test_a_text_codec_is_not(self, codec):
        assert im.is_image_sub_codec(codec) is False


class TestIsBonusFolder:
    """Film folder, or bonus material?

    A wrong answer either skips a film entirely or treats a two-minute trailer as
    a feature. The enum is DERIVED from the category table, and that derivation is
    the property worth pinning: every folder the classifier can sort an extra
    into has to be recognised here, whatever the table is revised to say.
    """

    @pytest.mark.parametrize("subfolder",
                             [name for name, _keywords in im.BONUS_CATEGORIES])
    def test_every_category_folder_is_bonus_material(self, subfolder):
        assert im.is_bonus_folder("./A Film (2020)/" + subfolder) is True

    def test_featurettes_is_too(self):
        """Not a row of the table - it is the folder extras land in before they
        are sorted into those."""
        assert im.is_bonus_folder("./A Film (2020)/Featurettes") is True

    @pytest.mark.parametrize("spelling", ["Extras", "extras", "EXTRAS",
                                          "Movie Extras"])
    def test_an_extras_folder_in_any_case(self, spelling):
        """What a release brings before anything has been renamed."""
        assert im.is_bonus_folder("./A Film (2020)/" + spelling) is True

    @pytest.mark.parametrize("folder", [
        "./A Film (2020)", "./The Other Guys (2010)",
        "./Trailers of Doom (2001)", "./Box Set/Film (1999)",
    ])
    def test_a_film_folder_is_not(self, folder):
        """Including the ones whose TITLE contains a category word: the match is
        on the END of the folder name, and a film folder ends in its year."""
        assert im.is_bonus_folder(folder) is False

    def test_a_trailing_slash_makes_no_difference(self):
        assert im.is_bonus_folder("./A Film (2020)/Featurettes/") is True


class TestIsLosslessTrackCodec:
    """Which Matroska audio tracks get an opus.

    Read from mkvmerge's codec_id, which wraps the codec in "A_..." and sometimes
    a suffix, so the enum is matched as a substring.
    """

    @pytest.mark.parametrize("codec", [
        "A_FLAC", "A_TRUEHD", "A_DTS", "A_DTS/EXPRESS", "A_PCM/INT/LIT",
        "A_PCM/FLOAT/IEEE", "A_APE",
    ])
    def test_a_lossless_track_codec_is_transcoded(self, codec):
        assert im.is_lossless_track_codec(codec) is True

    @pytest.mark.parametrize("codec", ["A_AC3", "A_EAC3", "A_AAC", "A_OPUS",
                                       "A_VORBIS", "A_MPEG/L3", ""])
    def test_a_lossy_one_is_not(self, codec):
        assert im.is_lossless_track_codec(codec) is False

    def test_the_codec_id_is_matched_case_insensitively(self):
        assert im.is_lossless_track_codec("a_flac") is True


class TestBonusCategoryFor:
    """Which Plex folder an extra belongs in. The table's order is the priority:
    the first matching category wins."""

    @pytest.mark.parametrize("name,expected", [
        ("The Making Of.mkv", "Behind The Scenes"),
        ("Deleted Scene 3.mkv", "Deleted Scenes"),
        ("Interview with the Director.mkv", "Interviews"),
        ("Theatrical Trailer.mkv", "Trailers"),
        ("Blooper Reel.mkv", "Scenes"),
        ("A Short.mkv", "Shorts"),
        ("Critical Review.mkv", "Other"),
    ])
    def test_a_keyword_lands_its_extra(self, name, expected):
        assert im.bonus_category_for(name) == expected

    def test_a_name_matching_nothing_stays_put(self):
        assert im.bonus_category_for("Feature Film.mkv") == ""

    def test_the_first_matching_row_wins(self):
        """"short" is the first row and "trailer" a later one, so a name holding
        both lands in Shorts."""
        assert im.bonus_category_for("Short Trailer.mkv") == "Shorts"


class TestLanguageAllowed:
    """Which languages the improved copy keeps: the first audio track's and
    English, plus the ones there is nothing to judge."""

    @pytest.mark.parametrize("language", ["eng", "en", "ENG"])
    def test_english_is_always_kept(self, language):
        assert im.language_allowed(language, "ger") is True

    def test_and_so_is_the_first_audio_tracks_own_language(self):
        assert im.language_allowed("ger", "ger") is True

    @pytest.mark.parametrize("language", ["", "null", "und"])
    def test_a_language_there_is_nothing_to_judge_is_kept(self, language):
        assert im.language_allowed(language, "ger") is True

    def test_no_linguistic_content_is_kept(self):
        """zxx is an isolated score or effects, not a second soundtrack."""
        assert im.language_allowed("zxx", "ger") is True

    @pytest.mark.parametrize("language", ["fra", "spa", "ita"])
    def test_any_other_language_is_excessive(self, language):
        assert im.language_allowed(language, "ger") is False


class TestReadTrackInfo:
    """The mkvmerge identification folded into per-track fields.

    What the shell did with a jq filter joining six fields on a unit separator,
    and the interesting part of it was ``tostring``: a property the file does not
    carry - no track name, no channel count on a subtitle, an absent commentary
    flag or language - renders as the literal string "null", which is what every
    comparison downstream is written against.
    """

    PAYLOAD = """{
      "tracks": [
        { "id": 0, "type": "video",
          "properties": { "codec_id": "V_MPEG4/ISO/AVC" } },
        { "id": 1, "type": "audio",
          "properties": { "codec_id": "A_TRUEHD", "audio_channels": 8,
                          "track_name": "Surround 7.1", "flag_commentary": false,
                          "language": "eng" } },
        { "id": 2, "type": "audio",
          "properties": { "codec_id": "A_AC3", "audio_channels": 6,
                          "flag_commentary": true } },
        { "id": 3, "type": "subtitles",
          "properties": { "codec_id": "S_TEXT/UTF8", "track_name": "English SDH",
                          "language": "ger" } }
      ]
    }"""

    @pytest.fixture
    def read(self, monkeypatch):
        class Done:
            returncode = 0
            stdout = self.PAYLOAD.encode()
        monkeypatch.setattr(im.subprocess, "run", lambda *a, **k: Done())
        return im.read_track_info("/does/not/matter.mkv")

    def test_every_track_is_read(self, read):
        names, _codecs, _channels, _comments, types, _langs = read
        assert len(names) == 4
        assert types == ["video", "audio", "audio", "subtitles"]

    def test_the_codecs_come_through_in_track_order(self, read):
        _names, codecs, _channels, _comments, _types, _langs = read
        assert codecs == ["V_MPEG4/ISO/AVC", "A_TRUEHD", "A_AC3",
                          "S_TEXT/UTF8"]

    def test_a_missing_name_is_the_literal_null_and_spaces_survive(self, read):
        names, _codecs, _channels, _comments, _types, _langs = read
        assert names == ["null", "Surround 7.1", "null", "English SDH"]

    def test_channels_are_null_where_the_track_type_has_none(self, read):
        _names, _codecs, channels, _comments, _types, _langs = read
        assert channels == ["null", "8", "6", "null"]

    def test_the_commentary_flag_keeps_its_three_states(self, read):
        _names, _codecs, _channels, comments, _types, _langs = read
        assert comments == ["null", "false", "true", "null"]

    def test_a_language_survives_verbatim(self, read):
        """Both the tagging and the commentary language read this field."""
        _names, _codecs, _channels, _comments, _types, langs = read
        assert langs == ["null", "eng", "null", "ger"]

    def test_a_file_that_cannot_be_identified_yields_no_tracks(self,
                                                               monkeypatch):
        """Which every caller already treats as nothing to do."""
        class Done:
            returncode = 2
            stdout = b"not json"
        monkeypatch.setattr(im.subprocess, "run", lambda *a, **k: Done())
        assert im.read_track_info("/x.mkv") == ([], [], [], [], [], [])

