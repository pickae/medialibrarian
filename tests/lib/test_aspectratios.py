"""Tests for medialib.lib.aspectratios - the video aspect-ratio buckets: what
SHAPE a coded pixel size is.

These say what the buckets are FOR, and pin the three things a rewrite would get
wrong: the boundaries are geometric, the coded size is what is measured, and the
canonical spellings win over the marketing ones.
"""

import math

import pytest

from medialib.lib import aspectratios as ar

pytestmark = pytest.mark.pure


class TestTheEverydayShapes:
    def test_hd(self):
        assert ar.bucket_of(1920, 1080) == "widescreen"

    def test_sd_television(self):
        assert ar.bucket_of(640, 480) == "fullscreen"

    def test_a_scope_master(self):
        assert ar.bucket_of(2048, 858) == "scope"

    def test_a_vertical_short(self):
        assert ar.bucket_of(1080, 1920) == "vertical"

    def test_the_display_value_is_what_a_report_holds(self):
        assert ar.label_of(1920, 1080) == "1.78:1"


class TestTheNearestBucketNotAnExactMatch:
    """A 2.39:1 film arrives as 1920x800, 1920x804 or 2048x858 and none of them is
    2.39 exactly. Exact matching would file almost every film under "other"."""

    @pytest.mark.parametrize("width,height", [(1920, 800), (1920, 804), (2048, 858)])
    def test_every_real_scope_master_lands_in_scope(self, width, height):
        assert ar.bucket_of(width, height) == "scope"

    def test_a_dci_flat_container_is_flat(self):
        assert ar.bucket_of(1998, 1080) == "flat"


class TestTheBoundariesAreGeometric:
    """An aspect ratio is a proportion, not a length: 1.33 is as far from 1.78 as
    1.78 is from 2.39, both being a factor of about 1.34. An arithmetic midpoint
    would sit closer to the wider bucket every time."""

    def test_each_boundary_is_the_geometric_mean_of_its_neighbours(self):
        for (low, high), (_, boundary) in zip(
            zip(ar.TABLE, ar.TABLE[1:], strict=False), ar.boundaries(), strict=True
        ):
            assert boundary == pytest.approx(math.sqrt(low.value * high.value))

    def test_a_size_just_inside_a_boundary_takes_the_narrower_bucket(self):
        _, boundary = next(
            (label, b) for label, b in ar.boundaries() if label == "1.78:1"
        )
        assert ar.bucket_of(round(boundary * 10000) - 1, 10000) == "widescreen"

    def test_and_just_outside_it_takes_the_wider_one(self):
        _, boundary = next(
            (label, b) for label, b in ar.boundaries() if label == "1.78:1"
        )
        assert ar.bucket_of(round(boundary * 10000) + 1, 10000) == "flat"

    def test_the_boundaries_ascend(self):
        values = [b for _, b in ar.boundaries()]
        assert values == sorted(values)


class TestTheLadderIsOpenAtBothEnds:
    """There is no "other": a shape far outside the table is more informative in
    the extreme bucket than in one that means nothing."""

    def test_a_banner_is_polyvision(self):
        assert ar.bucket_of(5000, 1000) == "polyvision"

    def test_a_tall_phone_crop_is_vertical(self):
        assert ar.bucket_of(1000, 3000) == "vertical"


class TestAnUnreadableSize:
    """Both dimensions are needed for a ratio, so - unlike a resolution tier -
    there is no falling back on the axis that was readable."""

    def test_a_zero_dimension_is_unknown_not_a_division_by_zero(self):
        assert ar.bucket_of(0, 1080) == ar.UNKNOWN
        assert ar.bucket_of(1920, 0) == ar.UNKNOWN

    def test_an_unreadable_dimension_is_unknown(self):
        assert ar.bucket_of("x", 1080) == ar.UNKNOWN

    def test_and_so_is_its_label(self):
        assert ar.label_of("", "") == ar.UNKNOWN


class TestNames:
    def test_a_key_resolves_to_itself(self):
        assert ar.named("widescreen") == "widescreen"

    def test_an_integer_ratio_resolves(self):
        assert ar.named("16:9") == "widescreen"

    def test_a_display_value_resolves_with_or_without_its_one(self):
        assert ar.named("1.78") == ar.named("1.78:1") == "widescreen"

    def test_a_marketing_name_resolves(self):
        assert ar.named("Techniscope") == "cinemaScope"

    def test_case_is_ignored(self):
        assert ar.named("PANAVISION") == ar.named("panavision")

    def test_an_approximate_marker_is_ignored(self):
        assert ar.named("~10:7") == ar.named("10:7") == "imaxFilm"

    def test_an_unknown_name_fails_so_a_typo_is_caught(self):
        assert ar.named("nope") is None
        assert ar.named("") is None


class TestTheCanonicalPassComesFirst:
    """Four rows have been called "Scope", and one of them is the bucket whose KEY
    is "scope". Without two passes that key would resolve to a different row, and
    the enum would lie about itself."""

    def test_the_key_scope_resolves_to_scope(self):
        assert ar.named("scope") == "scope"

    def test_though_the_marketing_name_scope_is_claimed_by_a_narrower_row(self):
        claimants = [b.key for b in ar.TABLE
                     if "scope" in (n.strip().lower() for n in b.names.split(","))]
        assert len(claimants) > 1
        assert claimants[0] != "scope"

    def test_every_key_resolves_to_itself(self):
        for bucket in ar.TABLE:
            assert ar.named(bucket.key) == bucket.key

    def test_every_offered_spelling_resolves(self):
        for bucket in ar.TABLE:
            assert ar.named(bucket.ratio) is not None
            assert ar.named(f"{bucket.normed}:1") is not None


class TestTheHeightAtAWidth:
    def test_a_scope_frame_in_a_1920_master(self):
        assert ar.height_at("scope", 1920) == 803

    def test_it_rounds_to_the_nearest_line_not_the_one_below(self):
        # 16:9 at 1919 is 1079.4, which rounds down; at 1921 it is 1080.6, up
        assert ar.height_at("16:9", 1919) == 1079
        assert ar.height_at("16:9", 1921) == 1081

    def test_a_bucket_it_does_not_know_fails(self):
        assert ar.height_at("nope", 1920) is None

    def test_a_width_that_is_not_a_width_fails(self):
        assert ar.height_at("scope", 0) is None
        assert ar.height_at("scope", "x") is None


class TestTheSqlIsGeneratedFromTheSameTable:
    def test_every_bucket_appears_as_its_display_value(self):
        sql = ar.bucket_sql("w", "h")
        for bucket in ar.TABLE:
            assert f"'{bucket.normed}:1'" in sql

    def test_narrowest_first_so_the_widest_needs_no_upper_bound(self):
        sql = ar.bucket_sql("w", "h")
        assert sql.index("'0.56:1'") < sql.index("'4.00:1'")
        assert sql.rstrip().endswith("END")

    def test_a_missing_or_zero_dimension_is_unknown(self):
        assert f"THEN '{ar.UNKNOWN}'" in ar.bucket_sql("w", "h")
        assert "w <= 0 OR h <= 0" in ar.bucket_sql("w", "h")


class TestTheEnumCarriesFourThingsPerBucket:
    """A bucket is a ratio, a display value, the names people call it, and where
    you meet it. Half an answer is what makes a report unreadable."""

    @pytest.mark.parametrize("key,ratio", [
        ("widescreen", "16:9"), ("scope", "239:100"),
        ("vertical", "9:16"), ("academy", "11:8"),
    ])
    def test_the_ratio_a_bucket_stands_for(self, key, ratio):
        assert ar.ratio_of(key) == ratio

    def test_the_names_say_what_the_shape_is_called(self):
        assert "HDTV" in (ar.names_of("widescreen") or "")
        assert "Storaro" in (ar.names_of("univisium") or "")

    def test_the_usage_says_where_you_meet_it(self):
        assert "Netflix" in (ar.usage_of("univisium") or "")
        assert "TikTok" in (ar.usage_of("vertical") or "")

    @pytest.mark.parametrize("bucket", [b.key for b in ar.TABLE])
    def test_every_bucket_carries_all_four(self, bucket):
        assert ar.ratio_of(bucket)
        assert ar.normed_of(bucket)
        assert ar.names_of(bucket)
        assert ar.usage_of(bucket)

    def test_a_bucket_that_is_not_one_carries_nothing(self):
        assert ar.ratio_of("nope") is None
        assert ar.normed_of("nope") is None
        assert ar.names_of("nope") is None
        assert ar.usage_of("nope") is None


class TestTheDisplayValuesSortIntoShapeOrder:
    """A pivot table orders a dimension's values as TEXT, which is the whole
    reason they are written to a fixed two decimals."""

    def test_sorting_them_as_text_leaves_them_where_they_were(self):
        labels = [ar.normed_of(name) for name in ar.bucket_names()]
        assert labels == sorted(labels)

    def test_and_the_names_come_back_narrowest_first(self):
        assert ar.bucket_names() == [bucket.key for bucket in ar.TABLE]
