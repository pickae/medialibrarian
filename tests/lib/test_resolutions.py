"""Tests for medialib.lib.resolutions - the video resolution ladder: what a
coded pixel size is called.

Why the ladder is shaped the way it is needs saying, and the two design choices
that look like mistakes until they are explained - either axis counts, and a cap
only ever scales down - are exactly the ones a rewrite would get wrong.
"""

import pytest

from medialib.lib import resolutions as res

pytestmark = pytest.mark.pure


class TestTheTierOfASize:
    def test_the_nominal_sizes(self):
        assert res.tier_of(1280, 720) == "720p"
        assert res.tier_of(1920, 1080) == "1080p"
        assert res.tier_of(3840, 2160) == "2160p"
        assert res.tier_of(7680, 4320) == "4320p"

    def test_one_pixel_short_is_the_tier_below(self):
        assert res.tier_of(1919, 1079) == "720p"

    def test_below_the_table_is_the_open_floor(self):
        assert res.tier_of(640, 480) == res.SUB_TIER

    def test_above_the_table_is_the_top_rung(self):
        assert res.tier_of(99999, 99999) == "4320p"


class TestEitherAxisCounts:
    """A frame that is not 16:9 fills one axis only, and still does that tier's
    worth of work. This is the rule the ladder exists to get right."""

    def test_a_four_by_three_camera_reaches_it_on_height(self):
        assert res.tier_of(2880, 2160) == "2160p"

    def test_a_scope_frame_reaches_it_on_width(self):
        assert res.tier_of(3840, 1600) == "2160p"

    def test_a_portrait_frame_is_classified_by_its_long_axis_as_a_height(self):
        # The rationale in the module is about landscape frames that fill one axis.
        # A portrait one turns that around: 1080x1920 has exactly a 1080p frame's
        # pixels, but its HEIGHT is 1920, which clears the 1440p rung's 1440. So a
        # vertical short is filed one tier above the landscape frame it is a
        # rotation of. That is what the code does and what a report shows; it is
        # recorded here so it is a known consequence rather than a surprise.
        assert res.tier_of(1080, 1920) == "1440p"
        assert res.tier_of(1920, 1080) == "1080p"


class TestAnUnreadableDimension:
    def test_neither_axis_readable_is_unknown_not_small(self):
        assert res.tier_of("", "") == res.UNKNOWN_TIER
        assert res.tier_of("x", None) == res.UNKNOWN_TIER

    def test_one_readable_axis_still_classifies(self):
        assert res.tier_of("x", 2160) == "2160p"

    def test_a_readable_zero_is_SD_not_unknown(self):
        assert res.tier_of("x", 0) == res.SUB_TIER

    def test_a_negative_or_fractional_dimension_is_not_a_dimension(self):
        assert res.tier_of("-1", "12.5") == res.UNKNOWN_TIER


class TestNamesAndAliases:
    def test_a_canonical_name_resolves_to_itself(self):
        assert res.named("1080p") == "1080p"

    def test_an_alias_resolves_to_the_canonical_name(self):
        assert res.named("UltraHD") == "2160p"

    def test_case_is_ignored_because_nobody_agrees_on_4k(self):
        assert res.named("4k") == res.named("4K") == "2160p"

    def test_the_floor_resolves_to_itself(self):
        assert res.named("sd") == res.SUB_TIER

    def test_an_unknown_name_is_a_typo_and_fails(self):
        assert res.named("1080") is None
        assert res.named("") is None

    def test_every_offered_spelling_is_an_accepted_one(self):
        # spellings() is what a caller prints when asked what it takes, so a name
        # it offers had better resolve
        for tier in res.TABLE:
            assert res.named(tier.name) == tier.name
            for alias in tier.aliases:
                assert res.named(alias) == tier.name


class TestTheCeiling:
    def test_a_tier_by_any_of_its_spellings(self):
        assert res.ceiling("2160p") == res.ceiling("UHD") == (3840, 2160)

    def test_the_open_floor_has_no_size_to_scale_to(self):
        assert res.ceiling(res.SUB_TIER) is None

    def test_neither_does_a_typo(self):
        assert res.ceiling("nope") is None


class TestCapping:
    def test_a_larger_source_is_scaled_down(self):
        assert res.capped(3840, 2160, "1080p") == (1920, 1080)

    def test_the_aspect_ratio_is_kept(self):
        assert res.capped(3840, 1600, "1080p") == (1920, 800)

    def test_a_smaller_source_is_never_blown_up(self):
        assert res.capped(1280, 720, "1080p") == (1280, 720)

    def test_sides_are_rounded_to_even_for_chroma_subsampling(self):
        width, height = res.capped(1919, 1079, "720p")
        assert width % 2 == 0 and height % 2 == 0

    def test_no_cap_asked_for_passes_through(self):
        assert res.capped(3840, 2160, "") == (3840, 2160)

    def test_a_tier_with_no_size_passes_through(self):
        assert res.capped(3840, 2160, res.SUB_TIER) == (3840, 2160)

    def test_an_unreadable_size_passes_through_unchanged(self):
        assert res.capped("x", "y", "1080p") == ("x", "y")

    def test_an_unscaled_size_keeps_the_caller_s_own_spelling(self):
        # bash hands the size to awk, which prints an unused -v assignment exactly
        # as given, so unchanged means unchanged down to the padding
        assert res.capped("0720", "0480", "1080p") == ("0720", "0480")


class TestTheSqlIsGeneratedFromTheSameTable:
    def test_every_tier_appears(self):
        sql = res.tier_sql("w", "h")
        for tier in res.TABLE:
            assert f"'{tier.name}'" in sql
            assert str(tier.width) in sql

    def test_highest_first_so_no_upper_bound_is_needed(self):
        sql = res.tier_sql("w", "h")
        assert sql.index("'4320p'") < sql.index("'720p'")

    def test_an_absent_row_is_unknown_and_a_small_one_is_the_floor(self):
        sql = res.tier_sql("w", "h")
        assert f"IS NULL AND h IS NULL THEN '{res.UNKNOWN_TIER}'" in sql
        assert f"ELSE '{res.SUB_TIER}'" in sql

    def test_the_width_only_form_asks_about_one_column(self):
        assert "h" not in res.width_sql("w").replace("THEN", "")

    def test_the_column_expression_is_pasted_verbatim(self):
        assert "COALESCE(v.width, 0) >= 1280" in res.tier_sql("v.width", "v.height")


class TestTheSelectableTiers:
    """What ``-r`` offers, which is not the same as what the table holds: the
    open-ended SD floor has no size, so it cannot be a cap."""

    def test_the_tiers_come_back_smallest_first(self):
        assert res.tier_names() == ["720p", "1080p", "1440p", "2160p", "4320p"]

    def test_the_open_floor_is_not_offered_as_a_selection(self):
        assert res.SUB_TIER not in res.tier_names()

    def test_every_offered_tier_has_a_size_to_cap_against(self):
        for name in res.tier_names():
            assert res.ceiling(name) is not None

    def test_the_spellings_line_offers_each_tier_with_its_aliases(self):
        offered = res.spellings()
        for tier in res.TABLE:
            assert tier.name in offered
