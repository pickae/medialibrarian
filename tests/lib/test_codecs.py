"""Tests for the shared video-codec table - medialib.lib.codecs.

What is pinned here is that a codec gets the SAME answer however it is asked
about: by its ffprobe spelling or by its family name, in any case, in the per
file lookup (the re-encode model) or in the generated SQL (the census, per
library). The numbers a family is WORTH in bitrate are not here - those are the
adequacy model's, and live with it.
"""

import re

import pytest

from medialib.lib import codecs


@pytest.fixture(scope="module", params=codecs.TABLE)
def a_table_row(request):
    return request.param


class TestTheFamilyLookup:
    def test_a_family_answers_its_own_name(self, a_table_row):
        family, _era, _aliases = a_table_row
        assert codecs.family_of(family) == family

    def test_every_alias_is_its_family(self, a_table_row):
        family, _era, aliases = a_table_row
        for alias in aliases:
            assert codecs.family_of(alias) == family

    @pytest.mark.parametrize("spelling", ["H265", "HEVC", "h265", "X264", "x264",
                                          "ProRes", "PRORES", "MPEG1VIDEO", "theora"])
    def test_in_what_case_it_arrived_in(self, spelling):
        assert codecs.family_of(spelling) in codecs.families()

    def test_the_dotted_I_lowers_the_shell_way(self):
        # bash lowercases character by character through the C library, which maps
        # U+0130 to a plain "i"; str.lower() would answer "i" plus a combining dot,
        # a different spelling and therefore a different miss. enums.shell_lower is
        # the one place that rule lives, and this pins that the port went through it.
        assert codecs.family_of("mpeg1vİdeo") == "mpeg2"

    @pytest.mark.parametrize("name", ["notACodec", "", " ", "  hevc", "hevc ",
                                      "x2651", "h26", "h2666", "mpeg3", "İ264",
                                      "L'Étranger"])
    def test_what_the_table_does_not_list_is_unknown(self, name):
        assert codecs.family_of(name) == codecs.UNKNOWN


class TestTheEraLookup:
    @pytest.mark.parametrize("name,era", [
        ("mpeg2video", codecs.MPEG2_ERA), ("mpeg1video", codecs.MPEG2_ERA),
        # an intra-only master compresses like that era too
        ("prores", codecs.MPEG2_ERA), ("dnxhd", codecs.MPEG2_ERA),
        ("mpeg4", codecs.MPEG4_ERA), ("theora", codecs.MPEG4_ERA),
        ("h264", codecs.MODERN_ERA), ("vc1", codecs.MODERN_ERA),
        ("av1", codecs.MODERN_ERA), ("vvc", codecs.MODERN_ERA),
    ])
    def test_the_generation_it_compresses_like(self, name, era):
        assert codecs.era_of(name) == era

    def test_an_unknown_is_unknown_not_modern(self):
        # both lookups say so outright rather than guessing, and the same marker
        # answers for both, so a caller can test either answer against it
        assert codecs.era_of("notACodec") == codecs.UNKNOWN
        assert codecs.era_of("") == codecs.UNKNOWN


class TestTheTableAsAList:
    def test_the_families_are_oldest_generation_first(self):
        assert codecs.families() == [row[0] for row in codecs.TABLE]
        assert codecs.families()[0] == "mpeg2"
        assert codecs.families()[-1] == "vvc"

    def test_the_oldest_row_whole(self):
        # the family, its era, and both MPEG spellings, oldest first
        assert codecs.TABLE[0] == ("mpeg2", "mpeg2Era",
                                   ("mpeg2video", "mpeg1video"))


class TestTheAliases:
    def test_a_family_names_the_spellings_that_mean_it(self):
        answer = codecs.aliases_of("hevc")
        assert " h265 " in f" {answer} "
        assert " x265 " in f" {answer} "

    def test_spaces_separate_them_not_commas(self, a_table_row):
        _family, _era, aliases = a_table_row
        assert codecs.aliases_of(_family) == " ".join(aliases)

    def test_a_family_with_one_alias_is_just_it(self):
        assert codecs.aliases_of("vp8") == "vp8"

    def test_a_name_that_is_not_a_family_has_none_to_give(self):
        assert codecs.aliases_of("notACodec") is None
        assert codecs.aliases_of("") is None


class TestTheSql:
    @pytest.fixture(scope="class")
    def family_sql(self):
        return codecs.family_sql("videoCodec")

    @pytest.fixture(scope="class")
    def era_sql(self):
        return codecs.era_sql("videoCodec")

    def test_it_generates_a_case_expression(self, family_sql):
        assert family_sql.startswith("CASE")
        assert family_sql.endswith("END")

    def test_the_null_and_empty_arm_comes_first(self, family_sql):
        first_when = family_sql.split("\n")[1]
        assert "IS NULL OR trim(videoCodec) = ''" in first_when
        assert "unknown" in first_when

    def test_it_maps_an_alias_to_its_family(self, family_sql):
        assert "IN ('hevc', 'h265', 'x265') THEN 'hevc'" in family_sql

    def test_it_matches_case_insensitively_as_bash_does(self, family_sql):
        assert "lower(trim(videoCodec))" in family_sql

    def test_it_answers_unknown_rather_than_guessing(self, family_sql):
        assert re.search(r"ELSE 'unknown'", family_sql)

    def test_the_rows_come_back_in_table_order(self, family_sql):
        when = family_sql.index("THEN 'mpeg2'")
        middle = family_sql.index("THEN 'h264'")
        last = family_sql.index("THEN 'vvc'")
        assert when < middle < last

    def test_the_family_is_written_out_of_its_own_alias_list(self, family_sql):
        # the bash builder skips an alias that is the family itself, so a
        # single-spelling family is exactly one entry, not two
        assert "IN ('vp8') THEN 'vp8'" in family_sql

    def test_era_sql_buckets_by_generation(self, era_sql):
        assert "IN ('mpeg4', 'msmpeg4v1', 'msmpeg4v2', 'msmpeg4v3', 'h263', 'flv1'," \
               " 'rv40', 'theora') THEN 'mpeg4Era'" in era_sql
        assert "IN ('intra', 'prores', 'dnxhd', 'mjpeg', 'ffv1', 'huffyuv', 'rawvideo',"\
               " 'dvvideo') THEN 'mpeg2Era'" in era_sql

    def test_era_sql_answers_three_buckets_and_the_unknown(self, era_sql):
        answers = {match for match in re.findall(r"THEN '([^']*)'", era_sql)}
        assert answers == {"mpeg2Era", "mpeg4Era", "modern", "unknown"}

    def test_family_sql_answers_one_per_family(self, family_sql):
        answers = {match for match in re.findall(r"THEN '([^']*)'", family_sql)}
        assert answers == set(codecs.families()) | {codecs.UNKNOWN}

    def test_the_expression_is_pasted_verbatim(self):
        # whatever the query holds the raw name in goes into the output
        # unescaped, quote and all
        assert "WHEN a'b IS NULL OR trim(a'b) = ''" in codecs.family_sql("a'b")


class TestTheEncoderMapping:
    @pytest.mark.parametrize("encoder,family", [
        ("libsvtav1", "av1"), ("av1_nvenc", "av1"),
        ("libx265", "hevc"), ("hevc_nvenc", "hevc"),
        ("libx264", "h264"), ("h264_nvenc", "h264"),
        ("libvpx-vp9", ""), ("libvpx", ""), ("nvenc", ""),
        ("", ""), (" ", ""),
    ])
    def test_what_an_encoder_produces(self, encoder, family):
        assert codecs.encoder_codec(encoder) == family

    def test_a_name_that_mentions_two_codecs_is_the_first_rules(self):
        # the rules are checked in order: av1, then hevc/265, then 264
        assert codecs.encoder_codec("hevc_av1") == "av1"
        assert codecs.encoder_codec("x264_av1") == "av1"
        assert codecs.encoder_codec("x265_x264") == "hevc"

    def test_an_encoder_name_is_a_substring_match(self):
        assert codecs.encoder_codec("libx2651") == "hevc"
        assert codecs.encoder_codec("x264x") == "h264"

    def test_the_matching_is_case_sensitive(self):
        # the shell's case patterns are, so an upper-case name is one this repo
        # does not use
        assert codecs.encoder_codec("AV1_NVENC") == ""

    def test_what_it_answers_is_a_family_the_table_knows(self):
        for encoder in ("libsvtav1", "libx265", "libx264"):
            family = codecs.encoder_codec(encoder)
            assert codecs.family_of(family) == family