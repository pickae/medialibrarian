"""The white box for medialib/lib/cuechapters.py.

What is pinned here is what a reader needs stated rather than inferred - the
frame clock, the hour rollover, and the two structural refusals that decide
whether a cue describes chapters at all.
"""

import pytest

from medialib.lib import cuechapters as cc

pytestmark = pytest.mark.pure


class TestTimeFromCueString:
    @pytest.mark.parametrize("text,expected", [
        ("1 00:00:00", 0),
        ("1 0:00:00", 0),
        ("1 0:0:00", 0),
        ("1 00:01:00", 1000),
        ("1 01:00:00", 60000),
        ("1 00:00:75", 1000),        # a full second of frames
        ("1 00:00:37", 493),         # 37/75 s, rounded
        ("1 00:00:38", 507),         # and the neighbour it must not equal
        ("1 03:25:37", 205493),
    ])
    def test_a_cue_time_is_frames_not_hundredths(self, text, expected):
        """The third field is 75ths of a second, so :37 is 493 ms and not 370."""
        assert cc.time_from_cue_string(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("1 08:00:00", 8 * 60 * 1000),
        ("1 00:09:00", 9 * 1000),
        ("1 00:00:09", 120),
    ])
    def test_a_leading_zero_is_still_base_ten(self, text, expected):
        """The shell reads 08 and 09 as octal unless it is told not to, and both
        are ordinary times in a cue sheet."""
        assert cc.time_from_cue_string(text) == expected

    def test_the_prefix_that_comes_along_is_dropped(self):
        assert cc.time_from_cue_string("01 00:01:00") == 1000

    def test_a_tail_that_is_all_time_keeps_its_first_field(self):
        """Ten characters off a >99-minute line leave no prefix to drop, and a
        parse that dropped one anyway would read the minutes as nothing."""
        assert cc.time_from_cue_string("123:00:00") == 123 * 60 * 1000

    def test_a_carriage_return_is_not_part_of_the_number(self):
        assert cc.time_from_cue_string("1 00:01:00\r") == 1000

    @pytest.mark.parametrize("frames", range(0, 75, 7))
    def test_every_frame_rounds_to_the_nearest_millisecond(self, frames):
        assert cc.time_from_cue_string("1 00:00:%02d" % frames) == round(
            frames * 1000 / 75)


class TestRows:
    def test_a_time_row_pads_all_four_fields(self):
        assert cc.time_row(1, 0) == "CHAPTER01=00:00:00.000"

    def test_the_hour_rolls_over(self):
        assert cc.time_row(7, 3661234) == "CHAPTER07=01:01:01.234"

    def test_a_chapter_past_the_hundredth_is_not_truncated(self):
        assert cc.time_row(100, 0) == "CHAPTER100=00:00:00.000"

    def test_a_name_row_carries_the_name_unaltered(self):
        assert cc.name_row(2, "A & B") == "CHAPTER02NAME=A & B"


def _cue(tmp_path, text):
    path = tmp_path / "in.cue"
    path.write_text(text)
    return str(path)


JOINED = '''FILE "joined.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Intro"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second"
    INDEX 01 03:25:37
'''


class TestChaptersFromCue:
    def test_one_joined_file_becomes_one_chapter_per_track(self, tmp_path):
        assert cc.chapters_from_cue(_cue(tmp_path, JOINED)) == [
            "CHAPTER01=00:00:00.000",
            "CHAPTER01NAME=Intro",
            "CHAPTER02=00:03:25.493",
            "CHAPTER02NAME=Second",
        ]

    def test_a_file_per_track_rip_describes_no_chapters(self, tmp_path):
        """Every track is time 0 of its own file, so there are no positions
        relative to a joined one - and a single 00:00:00 entry would be worse
        than nothing."""
        text = "".join(
            'FILE "t%02d.flac" WAVE\n  TRACK %02d AUDIO\n    TITLE "T%d"\n'
            "    INDEX 01 00:00:00\n" % (n, n, n) for n in (1, 2, 3))
        assert cc.chapters_from_cue(_cue(tmp_path, text)) == []

    def test_but_a_file_per_track_rip_with_real_offsets_is_kept(self, tmp_path):
        text = ('FILE "a.flac" WAVE\n  TRACK 01 AUDIO\n    TITLE "One"\n'
                "    INDEX 01 00:00:00\n"
                'FILE "b.flac" WAVE\n  TRACK 02 AUDIO\n    TITLE "Two"\n'
                "    INDEX 01 01:00:00\n")
        assert len(cc.chapters_from_cue(_cue(tmp_path, text))) == 4

    def test_a_data_track_never_becomes_a_chapter(self, tmp_path):
        text = ('FILE "disc.bin" BINARY\n  TRACK 01 MODE1/2352\n'
                '    TITLE "Data"\n    INDEX 01 00:00:00\n'
                'FILE "joined.flac" WAVE\n  TRACK 02 AUDIO\n'
                '    TITLE "Real"\n    INDEX 01 00:00:00\n')
        assert cc.chapters_from_cue(_cue(tmp_path, text)) == [
            "CHAPTER01=00:00:00.000", "CHAPTER01NAME=Real"]

    def test_the_album_header_title_is_not_a_chapter(self, tmp_path):
        text = 'TITLE "The Album"\n' + JOINED
        names = [line for line in cc.chapters_from_cue(_cue(tmp_path, text))
                 if "NAME=" in line]
        assert names == ["CHAPTER01NAME=Intro", "CHAPTER02NAME=Second"]

    def test_crlf_line_endings_read_the_same(self, tmp_path):
        assert (cc.chapters_from_cue(_cue(tmp_path, JOINED.replace("\n", "\r\n")))
                == cc.chapters_from_cue(_cue(tmp_path, JOINED)))

    def test_a_title_is_cleaned_the_way_every_other_name_is(self, tmp_path):
        """The quotes a cue puts round a title are not part of the chapter."""
        text = JOINED.replace('TITLE "Intro"', 'TITLE "01. Intro"')
        assert "CHAPTER01NAME=Intro" in cc.chapters_from_cue(_cue(tmp_path, text))


class TestWriteChaptersFromCue:
    def test_the_rows_are_written_one_per_line(self, tmp_path):
        out = tmp_path / "out.chapters"
        cc.write_chapters_from_cue(_cue(tmp_path, JOINED), str(out))
        assert out.read_text().splitlines() == cc.chapters_from_cue(
            _cue(tmp_path, JOINED))

    def test_a_cue_with_no_chapters_leaves_an_EMPTY_file(self, tmp_path):
        """Empty rather than absent: the caller's next step is to read it."""
        out = tmp_path / "out.chapters"
        text = "".join('FILE "t%d.flac" WAVE\n  TRACK 0%d AUDIO\n'
                       '    TITLE "T"\n    INDEX 01 00:00:00\n' % (n, n)
                       for n in (1, 2))
        cc.write_chapters_from_cue(_cue(tmp_path, text), str(out))
        assert out.exists() and out.read_text() == ""
