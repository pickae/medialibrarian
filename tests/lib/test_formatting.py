"""Tests for medialib.lib.formatting - the duration, clock, ratio and byte
formatters.

These say what the precision choices are for, and define what happens at the far
ends of the range - where the answer had better be a decision rather than a
surprise.
"""

import pytest

from medialib.lib.formatting import (
    awk_gt,
    awk_looks_numeric,
    awk_number,
    fmt_bytes,
    fmt_clock,
    fmt_hms,
    fmt_ratio,
)

pytestmark = pytest.mark.pure


class TestHowAStringBecomesANumber:
    """awk takes the longest numeric prefix. This is NOT how bash's (( )) reads the
    same text, and the difference has already been a bug: "0720" is 720 here and
    464 there."""

    def test_a_plain_number(self):
        assert awk_number("42") == 42

    def test_the_longest_numeric_prefix_wins(self):
        assert awk_number("12abc") == 12

    def test_text_that_starts_with_nothing_numeric_is_zero(self):
        assert awk_number("abc") == 0
        assert awk_number("") == 0

    def test_leading_space_and_sign_are_allowed(self):
        assert awk_number("  +5") == 5
        assert awk_number(" -3.5") == -3.5

    def test_scientific_notation_is_read(self):
        assert awk_number("1e3") == 1000

    def test_a_leading_zero_is_not_octal(self):
        assert awk_number("0720") == 720

    def test_hexadecimal_is_not_read(self):
        assert awk_number("0x10") == 0

    def test_the_words_inf_and_nan_are_not_numbers(self):
        assert awk_number("inf") == 0
        assert awk_number("nan") == 0


class TestDurations:
    def test_hms_always_shows_hours(self):
        assert fmt_hms(61) == "0:01:01"
        assert fmt_hms(3661) == "1:01:01"

    def test_the_clock_form_grows_an_hours_field_only_when_there_is_one(self):
        assert fmt_clock(61) == "1:01"
        assert fmt_clock(3661) == "1:01:01"

    def test_seconds_are_rounded_to_the_nearest(self):
        assert fmt_clock(59.4) == "0:59"
        assert fmt_clock(59.5) == "1:00"

    def test_a_negative_duration_is_zero_rather_than_a_negative_clock(self):
        assert fmt_hms(-1) == "0:00:00"
        assert fmt_clock(-3600) == "0:00"

    def test_unreadable_input_is_zero(self):
        assert fmt_clock("abc") == "0:00"


class TestTheSpeedUpRatio:
    """Two decimals below 10, one above. The same figure is reported for audio and
    video, three orders of magnitude apart: "60.00x" is noise and "0.3x" is coarse
    enough to hide a real improvement."""

    def test_below_ten_gets_two_decimals(self):
        assert fmt_ratio(0.3) == "0.30"
        assert fmt_ratio(9.994) == "9.99"

    def test_ten_and_above_gets_one(self):
        assert fmt_ratio(10) == "10.0"
        assert fmt_ratio(60) == "60.0"

    def test_the_switch_is_on_the_value_not_the_rendering(self):
        # 9.995 is below ten, so it takes the two-decimal branch even though it
        # renders as 10.00 (or 9.99 - the rounding is the C library's)
        assert fmt_ratio(9.995).count(".") == 1
        assert len(fmt_ratio(9.995).split(".")[1]) == 2

    def test_a_negative_ratio_is_below_ten(self):
        assert fmt_ratio(-1) == "-1.00"

    def test_nothing_measured_yet_reads_as_zero(self):
        # the row is drawn before the first sample, so this is the value it
        # renders for most of a file's first second
        assert fmt_ratio(0) == "0.00"
        assert fmt_ratio("") == "0.00"


class TestByteCounts:
    """Decimal units: what the census reports say, what the storage is sold as, and
    the only answer that stops two parts of the repo disagreeing about one library."""

    def test_under_a_kilobyte_is_whole_bytes(self):
        assert fmt_bytes(999) == "999 B"

    def test_the_unit_steps_at_each_power_of_a_thousand(self):
        assert fmt_bytes(1000) == "1.0 kB"
        assert fmt_bytes(1_000_000) == "1.0 MB"
        assert fmt_bytes(1_000_000_000) == "1.00 GB"

    def test_gigabytes_get_two_decimals_because_that_is_the_readable_one(self):
        assert fmt_bytes(1_524_908_032) == "1.52 GB"

    def test_a_byte_count_is_truncated_not_rounded_below_a_kilobyte(self):
        assert fmt_bytes(999.9) == "999 B"


class TestOutsideTheContract:
    """Past 2^63 the bash implementation stopped agreeing with this one: gawk gives
    up printing a double through a 64-bit integer, and at infinity it answers
    "+inf:-nan:-nan" for a duration. What happens beyond that point is a decision,
    so it is stated here rather than left untested."""

    def test_an_overflowing_value_reads_as_zero(self):
        # awk would carry the infinity into the formatter; carrying it here means
        # int(inf), which raises - and a formatter that crashes takes down a run
        # over a log line, which is worse than one that prints nonsense
        assert awk_number("1e400") == 0
        assert fmt_hms("1e400") == "0:00:00"
        assert fmt_bytes("1e400") == "0 B"

    def test_which_is_what_it_already_did_with_the_words(self):
        assert awk_number("inf") == awk_number("1e400") == 0

    def test_nothing_in_the_module_raises_on_any_of_them(self):
        for value in ("1e400", "-1e400", "inf", "nan", float("inf"), float("nan"), None):
            for render in (fmt_hms, fmt_clock, fmt_ratio, fmt_bytes):
                assert isinstance(render(value), str)

    def test_a_large_but_finite_value_is_carried_through(self):
        assert fmt_hms(1e15) == "277777777777:46:40"


class TestHowAwkDecidesToCompareAsNumbers:
    """awk compares two values numerically only when BOTH of them read as
    numbers; otherwise it compares them as text. The rule is not cosmetic: a
    duration ffprobe could not read is "N/A", and "N/A" > "60" holds, so the
    shell treats an unreadable file as an OVER-LONG one where a port that
    coerced both sides would call it empty. Every expectation below was taken
    from gawk 5.3 rather than from the specification."""

    @pytest.mark.parametrize("value", ["42", "0", "3.5", ".5", "-7", "1e3",
                                       " 70 ", "\t8\n", ""])
    def test_what_reads_as_a_number(self, value):
        assert awk_looks_numeric(value) is (value.strip() != "")

    @pytest.mark.parametrize("value", ["N/A", "abc", "70abc", "3600.0\n0",
                                       "0x10", "--3"])
    def test_and_what_does_not(self, value):
        assert awk_looks_numeric(value) is False

    def test_two_numbers_are_compared_as_numbers(self):
        assert awk_gt("3600.0", "60") is True
        assert awk_gt("60", "3600.0") is False
        assert awk_gt("0", "60") is False

    def test_a_value_that_is_not_a_number_is_compared_as_text(self):
        # "N" sorts after "6", so an unreadable duration is an over-long one
        assert awk_gt("N/A", "60") is True
        assert awk_gt("abc", "60") is True
        # ...and "3" sorts before "6", so the same is not true of every one
        assert awk_gt("3600.0\n0", "60") is False
        assert awk_gt("", "60") is False

    def test_a_numeric_constant_on_one_side_is_still_the_same_rule(self):
        assert awk_gt("N/A", 0) is True
        assert awk_gt("0", 0) is False
        assert awk_gt("", 0) is False
        # an integral constant is spelled as an integer in the text comparison,
        # which is why 180 loses to "abc" and not to "1"
        assert awk_gt(180.0, "abc") is False
        assert awk_gt(180.0, "90") is True
