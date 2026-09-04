"""Tests for medialib.lib.census - the field primitives every report row is built of.

What is pinned here is the handful of rules that are decisions rather than
mechanics: which shape of nothing a column holds, why a chapterless file counts
as one chapter, and the two escaping formats being genuinely different formats.
"""

import pytest

from medialib.lib import census


class TestMatchingAnExtension:
    @pytest.mark.parametrize("extension", ["mp3", "MP3", "Mp3", "mP3"])
    def test_the_case_of_either_side_does_not_matter(self, extension):
        assert census.extension_in(extension, ["MP3"])
        assert census.extension_in("mp3", [extension])

    def test_a_suffix_no_list_claims(self):
        assert not census.extension_in("mp3", ["flac", "opus"])

    def test_nothing_to_compare_against(self):
        assert not census.extension_in("mp3", [])

    def test_the_one_codepoint_the_two_languages_disagree_about(self):
        """U+0130, the dotted capital I. The shell lowers it to a plain "i" and
        Python's own str.lower() adds a combining dot, so a census built on the
        second would file MUZIK and müzik as two different extensions."""
        assert census.extension_in("İ", ["i"])
        assert census.extension_in("MÜZİK", ["müzik"])


class TestTheColumnsOfAReport:
    @pytest.mark.parametrize("report", ["audio", "video", "books", "comics"])
    def test_every_report_starts_with_the_path_and_the_size(self, report):
        assert census.columns(report).split(",")[:2] == ["path", "sizeBytes"]

    def test_a_type_there_is_no_report_for(self):
        assert census.columns("music") is None
        assert census.columns("") is None

    def test_the_type_is_matched_exactly(self):
        """The shell's case has no case-folding in it, so a capital is a miss."""
        assert census.columns("Audio") is None

    def test_the_units_are_in_the_names(self):
        """So a spreadsheet can sum a column without parsing it."""
        for report in census.COLUMNS:
            for name in census.COLUMNS[report]:
                assert not any(unit in name for unit in ("GiB", "MB", "kbps"))
        assert "durationSeconds" in census.COLUMNS["audio"]
        assert "sizeBytes" in census.COLUMNS["books"]


class TestJoiningARow:
    def test_the_plain_case(self):
        assert census.join(["a", "b", "c"]) == "a,b,c"

    def test_a_field_holding_the_separator_is_quoted(self):
        assert census.join(["a,b", "c"]) == '"a,b",c'

    def test_a_quote_is_doubled_and_the_field_wrapped(self):
        assert census.join(['he said "hi"']) == '"he said ""hi"""'

    def test_a_line_break_is_kept_whole(self):
        """RFC 4180 is lossless, which matters because the first column is a path
        and a path may hold a newline however rarely."""
        assert census.join(["two\nlines"]) == '"two\nlines"'

    def test_an_empty_first_field_still_takes_its_separator(self):
        """"nothing added yet" and "an empty first column" are different things,
        and treating them alike drops a separator - a row one column short from
        its second column on."""
        assert census.join(["", "b", "c"]) == ",b,c"

    def test_no_fields_at_all_is_an_empty_line(self):
        assert census.join([]) == ""

    def test_a_plain_row_is_not_marked_as_altered(self):
        assert not census.join(["a", "b"]).sanitised

    def test_quoting_is_not_altering(self):
        """The comma format is lossless, so nothing was lost and nothing is said."""
        assert not census.join(['a,b', '"']).sanitised


class TestJoiningARowWithTabs:
    def test_tab_mode_does_not_quote_at_all(self):
        """TSV has no agreed quoting convention, and a reader that meets a quote
        in one keeps it as data."""
        assert census.join(['he said "hi"'], "\t") == 'he said "hi"'

    @pytest.mark.parametrize("bad", ["\t", "\n", "\r"])
    def test_the_three_characters_that_would_break_the_row_become_spaces(self, bad):
        line = census.join([f"a{bad}b"], "\t")
        assert line == "a b"
        assert line.sanitised

    def test_and_the_row_says_that_it_happened(self):
        """Lossy, so the run can say once at the end instead of per row. Dropping
        the row instead would lose the file from the census entirely."""
        assert census.join(["plain", "with\ttab"], "\t").sanitised
        assert not census.join(["plain", "also plain"], "\t").sanitised

    def test_a_separator_that_is_neither_still_uses_the_comma_rules(self):
        assert census.join(["a;b"], ";") == '"a;b"'


class TestKeepingOnlyNumbers:
    @pytest.mark.parametrize("value", ["0", "1", "42", "007", "9223372036854775807"])
    def test_a_whole_number_is_kept_exactly_as_written(self, value):
        assert census.to_int(value) == value

    @pytest.mark.parametrize("value", ["N/A", "", " 5", "5 ", "-1", "1.0", "1e3", "+3"])
    def test_anything_else_becomes_empty_and_not_zero(self, value):
        """A bitrate of 0 and a bitrate nobody stated are different facts about a
        file, and empty is the one a spreadsheet reads as "not known"."""
        assert census.to_int(value) == ""


class TestCountingChapters:
    def test_a_file_with_no_chapter_marks_counts_as_one(self):
        """It is not a file with nothing in it - it is one unbroken chapter, which
        is what a player shows. Recorded as 0, a thousand unmarked audiobooks would
        total zero parts and a mean chapter length would divide by it."""
        assert census.to_chapters("0") == "1"

    def test_and_so_does_a_zero_written_at_length(self):
        assert census.to_chapters("000") == "1"

    def test_a_real_count_is_left_exactly_as_written(self):
        """Reading it as decimal is what stops a leading zero being taken for
        octal, not a decision to normalise the column."""
        assert census.to_chapters("007") == "007"
        assert census.to_chapters("12") == "12"

    @pytest.mark.parametrize("value", ["N/A", "", "-1", "1.5"])
    def test_a_count_that_is_not_a_number_is_still_unknown(self, value):
        """"One chapter" is an assertion about a file that was read, and unknown
        is not."""
        assert census.to_chapters(value) == ""


class TestDurations:
    def test_three_decimals(self):
        assert census.to_seconds("4923.264") == "4923.264"

    def test_a_whole_number_gains_them(self):
        assert census.to_seconds("60") == "60.000"

    def test_ffprobes_six_decimals_lose_the_three_that_are_noise(self):
        assert census.to_seconds("4923.264000") == "4923.264"

    @pytest.mark.parametrize("value", ["N/A", "", "-1.5", "1e3", ".5", "5."])
    def test_anything_that_is_not_a_plain_decimal_is_unknown(self, value):
        assert census.to_seconds(value) == ""


class TestTheRoundingTheShellActuallyDoes:
    """bash's printf reads with strtold, so what it rounds is the nearest 80-bit
    float and not the nearest double Python parses the same text into.

    On a value sitting on the boundary the two disagree in BOTH directions, so
    neither "round the exact decimal" nor "round through a Python float" is the
    rule. Each value below is one where they part company.
    """

    @pytest.mark.parametrize("value,expected", [
        ("4923.2645", "4923.264"),
        ("42445.9705", "42445.971"),
        ("19772.4045", "19772.405"),
        ("9156.2465", "9156.247"),
        ("55642.0605", "55642.061"),
        ("66510.2195", "66510.219"),
    ])
    def test_a_half_way_value_goes_where_the_shell_puts_it(self, value, expected):
        assert census.to_seconds(value) == expected

    def test_and_an_ordinary_value_is_not_disturbed_by_any_of_that(self):
        assert census.to_seconds("1.5") == "1.500"
        assert census.to_seconds("0.0005") == "0.000"


class TestFrameRates:
    def test_the_ntsc_rates_are_rationals_and_are_divided_here(self):
        """23.976 is a rounding of 24000/1001, not the other way round - the
        container stores the fraction because that is what the rate genuinely is."""
        assert census.to_frame_rate("24000/1001") == "23.976"
        assert census.to_frame_rate("30000/1001") == "29.970"
        assert census.to_frame_rate("60000/1001") == "59.940"

    def test_a_whole_rate(self):
        assert census.to_frame_rate("25/1") == "25.000"
        assert census.to_frame_rate("50/2") == "25.000"

    def test_the_rate_ffprobe_prints_when_it_could_not_work_one_out(self):
        assert census.to_frame_rate("0/0") == ""

    def test_a_denominator_of_zero_is_not_an_error_either(self):
        assert census.to_frame_rate("1/0") == ""

    def test_a_numerator_of_zero_is_unknown_rather_than_a_rate_of_zero(self):
        assert census.to_frame_rate("0/5") == ""

    def test_a_rate_already_written_as_a_decimal_is_taken_as_one(self):
        assert census.to_frame_rate("23.976") == "23.976"
        assert census.to_frame_rate("25") == "25.000"

    def test_but_a_decimal_that_rounds_to_nothing_is_unknown(self):
        """Only the decimal spelling is checked for zero; the rational one has
        already refused a zero numerator."""
        assert census.to_frame_rate("0") == ""
        assert census.to_frame_rate("0.0004") == ""

    @pytest.mark.parametrize("value", ["N/A", "", "1/", "/1001", "-24/1", "24 / 1"])
    def test_anything_else_is_unknown(self, value):
        assert census.to_frame_rate(value) == ""


class TestTheFileSize:
    def test_the_raw_byte_count(self, tmp_path):
        target = tmp_path / "x.bin"
        target.write_bytes(b"0123456789")
        assert census.file_size(str(target)) == "10"

    def test_an_empty_file_is_a_zero_and_not_an_unknown(self, tmp_path):
        target = tmp_path / "empty"
        target.write_bytes(b"")
        assert census.file_size(str(target)) == "0"

    def test_a_file_that_is_not_there_has_no_size(self, tmp_path):
        assert census.file_size(str(tmp_path / "nope")) == ""
