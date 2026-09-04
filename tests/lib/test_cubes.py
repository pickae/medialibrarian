"""Tests for medialib.lib.cubes - what a census hypercube is made of, as SQL.

What is pinned here is what the SQL MEANS: the properties a reader of the cube
depends on, which a diff of two identical strings cannot tell you were ever
intended.
"""

import re

import pytest

from medialib.lib import codecs, cubes


class TestWhichReportIsWhich:
    @pytest.mark.parametrize("name,expected", [
        ("videoFilms.csv", "video"), ("audioMusic.csv", "audio"),
        ("booksNovels.csv", "books"), ("comicsScans.tsv", "comics"),
        ("/library/reports/videoSeries.tsv", "video"),
        ("AUDIOPodcasts.CSV", "audio"),
    ])
    def test_the_prefix_names_the_type(self, name, expected):
        assert cubes.report_type(name) == expected

    @pytest.mark.parametrize("name", ["videoFilms.txt", "notatype.csv", "", ".csv",
                                      "videoFilms", "videoFilms.csv.bak"])
    def test_a_name_no_type_claims(self, name):
        assert cubes.report_type(name) == ""

    def test_only_a_csv_or_a_tsv_is_a_report(self):
        assert cubes.report_type("videoFilms.csv") == "video"
        assert cubes.report_type("videoFilms.json") == ""

    def test_a_windows_path_is_split_on_its_own_separator(self):
        assert cubes.report_type("C:\\reports\\videoA.csv") == "video"

    @pytest.mark.parametrize("name,expected", [
        ("videoFilms.csv", "Films"), ("audioMusic.csv", "Music"),
        ("/a/b/audio Old Stuff.csv", " Old Stuff"), ("video.csv", ""),
        ("AUDIOPodcasts.CSV", "Podcasts"),
    ])
    def test_the_rest_of_the_name_is_the_library(self, name, expected):
        assert cubes.report_library(name) == expected

    def test_the_library_keeps_its_own_case(self):
        """The prefix is matched case-insensitively but the library is not a
        bucket name - it is what the person called their library."""
        assert cubes.report_library("VIDEOFilms.csv") == "Films"

    def test_a_report_of_no_type_has_no_library(self):
        assert cubes.report_library("notatype.csv") == ""


class TestTheTables:
    @pytest.mark.parametrize("content", ["audio", "video", "books", "comics"])
    def test_every_report_starts_with_the_path_and_the_size(self, content):
        assert cubes.column_names(content).split()[:2] == ["path", "sizeBytes"]

    @pytest.mark.parametrize("content", ["audio", "video", "books", "comics"])
    def test_the_columns_agree_with_what_the_census_writes(self, content):
        """One definition in this repo: a column added to a report without being
        added here is meant to be a refusal at startup, not a cube that drops it."""
        from medialib.lib import census
        assert cubes.column_names(content).split() == list(census.COLUMNS[content])

    @pytest.mark.parametrize("content", ["audio", "video", "books", "comics"])
    def test_every_cube_is_split_by_library(self, content):
        """The one axis all four share, so the four can be read side by side."""
        assert cubes.DIMENSIONS[content][0] == "library"

    @pytest.mark.parametrize("content", ["audio", "video", "books", "comics"])
    def test_every_cube_counts_files_first(self, content):
        assert cubes.MEASURES[content][0] == "files"

    @pytest.mark.parametrize("content", ["audio", "video", "books", "comics"])
    def test_a_measure_is_never_also_a_dimension(self, content):
        """A column cannot be both summed and grouped by in the same cube."""
        assert not set(cubes.MEASURES[content]) & set(cubes.DIMENSIONS[content])

    @pytest.mark.parametrize("name", ["nonsense", "", "Audio", "music"])
    def test_a_type_there_is_no_report_for(self, name):
        assert cubes.column_spec(name) is None
        assert cubes.column_names(name) is None
        assert cubes.fact_sql(name) is None
        assert cubes.cube_sql(name) is None
        assert cubes.totals_sql(name) is None
        assert cubes.export_sql(name, "/out") is None


class TestQuotingForSql:
    def test_an_apostrophe_is_doubled(self):
        """The only escape a SQL string literal has, and what carries a path
        holding an apostrophe into a query unharmed."""
        assert cubes.sql_string("it's") == "'it''s'"

    def test_a_plain_string(self):
        assert cubes.sql_string("/a/b.csv") == "'/a/b.csv'"

    def test_an_empty_string_is_still_quoted(self):
        assert cubes.sql_string("") == "''"

    def test_a_backslash_is_not_an_escape_here(self):
        assert cubes.sql_string("a\\b") == "'a\\b'"


class TestTheDimensionFragments:
    def test_a_text_dimension_is_trimmed_and_lowered(self):
        """A dimension is a bucket name, and "AAC" and "aac" are one bucket. The
        raw table keeps the original spelling for anyone who wants it."""
        got = cubes.text_dimension("codec")
        assert "lower(trim(codec))" in got and "'unknown'" in got

    def test_a_number_dimension_becomes_text(self):
        """Every dimension has to be able to hold the "ALL" of a rolled-up level
        and the "unknown" of a value the census could not read."""
        assert "CAST(channels AS VARCHAR)" in cubes.number_dimension("channels")

    def test_every_dimension_has_an_unknown(self):
        for fragment in (cubes.text_dimension("x"), cubes.number_dimension("x"),
                         cubes.suffix_dimension("x"), cubes.library_dimension("audio"),
                         cubes.dynamic_range_case("x"), cubes.hfr_case("x")):
            assert "'unknown'" in fragment

    def test_dolby_vision_is_tested_before_hdr(self):
        """A Dolby Vision file is also an HDR10 or HLG file in every case but
        profile 5, and the axis has to say which of the three it will be PLAYED
        as. Order is the whole rule."""
        got = cubes.dynamic_range_case("dynamicRange")
        assert got.index("'DV'") < got.index("'HDR'")

    def test_nothing_falls_through_to_a_guess(self):
        assert cubes.dynamic_range_case("x").rstrip().endswith("ELSE 'unknown'\n        END")

    def test_high_frame_rate_is_strictly_over_thirty(self):
        """So 29.97 and 30 are "no" and 48, 50 and 60 are "yes"."""
        assert "> 30" in cubes.hfr_case("frameRateFps")


class TestTheBitrateLadders:
    @pytest.mark.parametrize("ladder", ["audio", "video"])
    def test_the_labels_sort_as_text_into_their_own_order(self, ladder):
        """A pivot table orders a dimension's values alphabetically, under which
        "<120kbps" comes before "<36kbps" and the band reads backwards. The zero
        padding is what stops that, so it is the property worth pinning."""
        got = cubes.bitrate_tier_case("b", ladder)
        labels = re.findall(r"'([<>][0-9.]+[kM]bps)'", got)
        assert labels == sorted(labels)

    @pytest.mark.parametrize("ladder", ["audio", "video"])
    def test_the_open_ended_top_band_sorts_last(self, ladder):
        got = cubes.bitrate_tier_case("b", ladder)
        labels = re.findall(r"'([<>][0-9.]+[kM]bps)'", got)
        assert labels[-1].startswith(">")
        assert all(label.startswith("<") for label in labels[:-1])

    @pytest.mark.parametrize("ladder", ["audio", "video"])
    def test_the_thresholds_only_ever_go_up(self, ladder):
        got = cubes.bitrate_tier_case("b", ladder)
        thresholds = [int(n) for n in re.findall(r"< ([0-9]+) THEN", got)]
        assert thresholds == sorted(thresholds)

    def test_the_two_ladders_are_two_orders_of_magnitude_apart(self):
        """Feeding the audio numbers to a film would put every single file in the
        top band and the axis would answer nothing."""
        audio = [int(n) for n in re.findall(
            r"< ([0-9]+) THEN", cubes.bitrate_tier_case("b", "audio"))]
        video = [int(n) for n in re.findall(
            r"< ([0-9]+) THEN", cubes.bitrate_tier_case("b", "video"))]
        assert min(video) > max(audio)

    def test_a_ladder_there_is_none_of(self):
        assert cubes.bitrate_tier_case("b", "books") is None


class TestTheWeightedBitrate:
    def test_both_filters_are_the_same_condition(self):
        """Numerator and denominator must be taken over exactly the same files, or
        one with a duration but no stated bitrate would add seconds and no bits and
        pull the answer down. Same condition, and here the same TEXT."""
        got = cubes.weighted_bitrate("br", "dur")
        filters = re.findall(r"FILTER \(WHERE ([^)]*)\)", got)
        assert len(filters) == 2
        assert filters[0] == filters[1]

    def test_the_empty_bucket_reads_as_unknown_and_not_as_zero(self):
        assert "NULLIF(SUM(dur)" in cubes.weighted_bitrate("br", "dur")


class TestBuildingTheCube:
    def test_every_combination_of_the_axes_appears_once(self):
        """GROUP BY CUBE, not ROLLUP: these axes are not a hierarchy, so what is
        wanted is every combination and each exactly once."""
        got = cubes.cube_sql("audio")
        sets = re.findall(r"GROUPING SETS \((.*)\);", got)
        elements = []
        for group in sets:
            elements += re.findall(r"\(([^)]*)\)", group)
        assert len(elements) == 2 ** len(cubes.DIMENSIONS["audio"])
        assert len(set(elements)) == len(elements)

    def test_the_grand_total_is_the_first_grouping_set(self):
        """Batches go out coarsest first, so the finished table is already in
        depth order and the total is its first row."""
        got = cubes.cube_sql("audio")
        assert re.search(r"GROUPING SETS \(\(\)", got)

    def test_the_first_statement_creates_and_the_rest_insert(self):
        got = cubes.cube_sql("video", chunk_size=32)
        assert got.startswith("CREATE OR REPLACE TABLE videoCube AS")
        assert got.count("CREATE OR REPLACE TABLE") == 1
        assert got.count("INSERT INTO videoCube") == got.count("GROUPING SETS") - 1

    def test_the_batch_size_decides_how_many_statements(self):
        """The eleven-axis video cube is 2048 groupings. One statement per 32 of
        them is the whole reason this is batched - a single GROUP BY CUBE builds
        all 2048 aggregations at once, each with its own hash table per thread."""
        total = 2 ** len(cubes.DIMENSIONS["video"])
        for chunk in (1, 2, 7, 32, 100):
            got = cubes.cube_sql("video", chunk_size=chunk)
            expected = -(-total // chunk)
            assert got.count("GROUPING SETS") == expected

    def test_a_batch_size_past_the_count_gives_one_statement(self):
        got = cubes.cube_sql("books", chunk_size=5000)
        assert got.count("GROUPING SETS") == 1
        assert "INSERT INTO" not in got

    def test_an_axis_no_set_in_this_batch_groups_by_is_written_as_the_literal(self):
        """Only the axes some grouping set in THIS statement mentions may be named
        in its select list; the rest are rolled up in every one of its rows."""
        got = cubes.cube_sql("audio", chunk_size=1)
        first = got.split(";")[0]
        assert first.count("'ALL' AS ") == len(cubes.DIMENSIONS["audio"])
        assert "COALESCE(" not in first.split("FROM")[0]

    def test_depth_counts_the_axes_that_are_not_rolled_up(self):
        got = cubes.cube_sql("books", chunk_size=5000)
        assert "CASE WHEN library IS NULL THEN 0 ELSE 1 END" in got
        assert "AS depth" in got

    def test_the_grand_total_batch_has_a_depth_of_zero(self):
        got = cubes.cube_sql("audio", chunk_size=1)
        assert "    0 AS depth" in got.split(";")[0]

    def test_the_measures_read_the_same_way_at_every_level(self):
        got = cubes.cube_sql("comics", chunk_size=1)
        statements = [s for s in got.split(";") if "SELECT" in s]
        measures = ["COUNT(*) AS files", "SUM(sizeBytes)", "SUM(pages)"]
        for statement in statements:
            for measure in measures:
                assert measure in statement


class TestTheLastTwoStatements:
    def test_the_total_is_read_from_the_cube_and_not_computed_again(self):
        """Which is the point of having built it."""
        got = cubes.totals_sql("audio")
        assert "FROM audioCube WHERE depth = 0" in got
        assert "GROUP BY" not in got

    def test_the_export_is_ordered_so_two_runs_can_be_diffed(self):
        got = cubes.export_sql("books", "/out")
        assert "ORDER BY depth, library, format" in got

    def test_a_trailing_slash_on_the_directory_is_not_doubled(self):
        assert cubes.export_sql("books", "/out/") == cubes.export_sql("books", "/out")

    def test_the_export_path_is_quoted_as_a_sql_literal(self):
        assert "'/it''s/booksCube.csv'" in cubes.export_sql("books", "/it's")


class TestLoadingTheReports:
    def test_an_empty_cell_becomes_a_null(self):
        """Which is the whole point of the census leaving it empty - a 0 and a
        value nobody stated are different facts."""
        assert "nullstr = ''" in cubes.load_sql("books", ["/a/b.csv"])

    def test_a_tsv_is_read_with_no_quoting_at_all(self):
        """There is no agreed TSV quoting, so the reader must not treat a quote in
        a path as one either."""
        assert "quote = '', escape = ''" in cubes.load_sql("books", ["/a/b.tsv"], "\t")
        assert "quote = '\"'" in cubes.load_sql("books", ["/a/b.csv"], ",")

    def test_every_report_of_a_type_is_read_into_one_table(self):
        got = cubes.load_sql("audio", ["/a/audioA.csv", "/b/audioB.csv"])
        assert "['/a/audioA.csv', '/b/audioB.csv']" in got

    def test_the_filename_column_is_asked_for(self):
        """It is what the library dimension is read back out of, so several
        reports of one type can share a table and still be told apart."""
        assert "filename = true" in cubes.load_sql("audio", ["/a/x.csv"])

    def test_the_column_types_are_given_rather_than_sniffed(self):
        """A report whose first thousand rows happen to have no bitrate would
        otherwise be read as a text column and then refuse to be summed."""
        got = cubes.load_sql("audio", ["/a/x.csv"])
        assert "'bitrateBitsPerSecond': 'BIGINT'" in got

    def test_a_type_there_is_no_report_for(self):
        assert cubes.load_sql("nonsense", ["/a/x.csv"]) is None


class TestTheFactView:
    """One row per file, every dimension already bucketed and never null. It is
    what the cube is built from and what to query when a bucket looks wrong."""

    @pytest.mark.parametrize("content", ["audio", "video", "books", "comics"])
    def test_it_defines_every_axis_the_cube_groups_by(self, content):
        view = cubes.fact_sql(content)
        for axis in cubes.DIMENSIONS[content]:
            assert "AS %s," % axis in view or "AS %s\n" % axis in view, axis

    def test_the_codec_family_is_the_shared_librarys_expression(self):
        """The rule exists once. The census buckets h265 and hevc together
        because it pastes videoCodecs' own CASE in, not because somebody wrote a
        second one that happens to agree today."""
        view = cubes.fact_sql("video")
        assert codecs.family_sql("videoCodec") in view
        assert codecs.era_sql("videoCodec") in view

    def test_and_what_the_file_actually_said_survives_beside_them(self):
        view = cubes.fact_sql("video")
        assert "AS videoCodec," in view
        assert "AS videoCodecFamily," in view
        assert "AS videoCodecEra," in view

    def test_a_type_there_is_no_report_for_has_no_view(self):
        assert cubes.fact_sql("nope") is None
