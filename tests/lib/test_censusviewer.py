"""The white box for medialib/lib/censusviewer.py.

What is pinned here is the properties of the page rather than its bytes.

The properties are the ones the module's own head argues for. Every measure must
be additive, because a browser folding buckets into bigger buckets can only add.
Every column of the export must be declared, because a pivot engine cannot infer
the type of a column a library left entirely empty. And the grain must be read
from the FACTS view rather than the cube, because the cube holds every level of
roll-up at once.
"""

import re

import pytest

from medialib.lib import censusviewer as v
from medialib.lib import cubes

pytestmark = pytest.mark.pure

TYPES = ("audio", "video", "books", "comics")


class TestTheGrainIsTheFactsAndNotTheCube:
    """The whole point of the base grain: one row per combination of dimension
    values, read from the per-file facts view, and not one row per grouping set
    of the cube."""

    @pytest.mark.parametrize("content", TYPES)
    def test_the_grain_selects_from_the_facts_view(self, content):
        sql = v.viewer_grain_sql(content)
        assert "FROM %sFacts" % content in sql
        assert "Cube" not in sql

    @pytest.mark.parametrize("content", TYPES)
    def test_it_groups_by_every_axis_and_nothing_else(self, content):
        sql = v.viewer_grain_sql(content)
        grouped = sql.split("GROUP BY ")[1].strip()
        assert grouped.split(", ") == v.viewer_dimensions(content).split()

    @pytest.mark.parametrize("content", TYPES)
    def test_the_file_count_is_a_real_count(self, content):
        """COUNT(*) and not a literal 1: a row here already stands for however
        many files fell into that combination."""
        assert "COUNT(*) AS files" in v.viewer_grain_sql(content)

    @pytest.mark.parametrize("content", TYPES)
    def test_no_path_column_reaches_the_export(self, content):
        """A path is unique per file, so carrying it would make every bucket hold
        exactly one file and there would be no aggregation left to do."""
        assert "path" not in v.viewer_grain_columns(content).split()

    def test_an_unknown_type_has_no_grain_at_all(self):
        assert v.viewer_grain_sql("bogus") is None
        assert v.viewer_grain_columns("bogus") is None
        assert v.viewer_dimensions("bogus") is None
        assert v.viewer_measures("bogus") is None


class TestEveryMeasureIsAdditive:
    """A browser folding buckets into bigger buckets can only add, so a measure
    that is not additive cannot be a measure here."""

    @pytest.mark.parametrize("content", TYPES)
    def test_the_measures_are_sums_or_the_count(self, content):
        sql = v.viewer_grain_sql(content)
        for measure in v.viewer_measures(content).split():
            if measure == "files":
                assert "COUNT(*) AS files" in sql
            else:
                assert "AS %s" % measure in sql
                clause = sql.split("AS %s" % measure)[0].rsplit("\n", 1)[1]
                assert clause.strip().startswith("SUM(")

    @pytest.mark.parametrize("content", TYPES)
    def test_no_bitrate_is_a_measure(self, content):
        """A bitrate is bits per second: no sum is right, and a plain average
        would weight a twelve-second clip like a three-hour film. It is BANDED
        into an axis instead."""
        for measure in v.viewer_measures(content).split():
            assert "itrate" not in measure

    def test_and_the_bands_are_axes_where_a_bitrate_exists(self):
        assert "bitrateTier" in v.viewer_dimensions("audio").split()
        assert "videoBitrateTier" in v.viewer_dimensions("video").split()

    @pytest.mark.parametrize("content", TYPES)
    def test_the_file_count_comes_first(self, content):
        """It is the one every question starts with."""
        assert v.viewer_measures(content).split()[0] == "files"


class TestThePagesUnitsAreThePages:
    """The census keeps bytes and seconds, because those are exact whole numbers.
    Only the page is handed gigabytes and hours, and both are a multiplication by
    a constant - which is what makes them safe where every roll-up is an
    addition."""

    @pytest.mark.parametrize("content", TYPES)
    def test_sizes_reach_the_page_as_gigabytes(self, content):
        sql = v.viewer_grain_sql(content)
        assert "SUM(sizeBytes) / %s AS sizeGigabytes" % v.BYTES_PER_GIGABYTE \
            in sql
        assert "sizeGigabytes" in v.viewer_measures(content).split()
        assert "sizeBytes" not in v.viewer_measures(content).split()

    @pytest.mark.parametrize("content", ("audio", "video"))
    def test_durations_reach_it_as_hours(self, content):
        sql = v.viewer_grain_sql(content)
        assert "SUM(durationSeconds) / %s AS durationHours" \
            % v.SECONDS_PER_HOUR in sql

    def test_the_gigabyte_is_the_decimal_one(self):
        """10^9 and not 2^30, because the number this is compared against is a
        disk's stated capacity."""
        assert v.BYTES_PER_GIGABYTE == "1000000000.0"

    def test_and_the_hour_is_an_hour(self):
        assert v.SECONDS_PER_HOUR == "3600.0"


class TestEveryColumnIsDeclared:
    """A census column is empty when nobody stated the value - a library whose
    books are all epubs has an entirely empty "pages" column - and a pivot engine
    that cannot infer a column of nothing refuses the whole table."""

    @pytest.mark.parametrize("content", TYPES)
    def test_the_schema_declares_exactly_the_export_s_columns(self, content):
        schema = v.viewer_schema(content)
        declared = [part.split('":"')[0].strip('"')
                    for part in schema.strip("{}").split(',"')]
        assert declared == v.viewer_grain_columns(content).split()

    @pytest.mark.parametrize("content", TYPES)
    def test_every_axis_is_a_string_and_every_measure_a_number(self, content):
        for axis in v.viewer_dimensions(content).split():
            assert v.viewer_column_type(axis) == "string"
        for measure in v.viewer_measures(content).split():
            assert v.viewer_column_type(measure) in ("integer", "float")

    @pytest.mark.parametrize("column", ("sizeBytes", "sizeGigabytes",
                                        "durationSeconds", "durationHours",
                                        "words", "characters"))
    def test_anything_summed_that_can_grow_is_a_float(self, column):
        """Perspective's "integer" is 32 bits, which a media library overruns on
        its first measure: 2^31 bytes is 2GB, and the point of summing sizeBytes
        is to get numbers far past that."""
        assert v.viewer_column_type(column) == "float"

    @pytest.mark.parametrize("column", ("files", "chapters", "pages"))
    def test_only_the_genuinely_small_counts_stay_integers(self, column):
        """So they read as "12" and not "12.00"."""
        assert v.viewer_column_type(column) == "integer"

    def test_an_unknown_type_declares_an_empty_schema_rather_than_refusing(self):
        """The shell reads its columns through a command substitution, which
        swallows the refusal, so the loop runs over nothing and "{}" is printed.
        An empty schema is a tab Perspective rejects on its own, which is not the
        same failure as a page that was never written."""
        assert v.viewer_schema("bogus") == "{}"


class TestTheOpeningPivot:
    @pytest.mark.parametrize("content", TYPES)
    def test_a_tab_opens_grouped_by_exactly_one_axis(self, content):
        axis = v.viewer_opening_axis(content)
        assert axis in v.viewer_dimensions(content).split()
        assert " " not in axis

    @pytest.mark.parametrize("content", TYPES)
    def test_it_opens_showing_only_columns_it_carries(self, content):
        for column in v.viewer_opening_columns(content).split():
            assert column in v.viewer_measures(content).split()

    @pytest.mark.parametrize("content", TYPES)
    def test_every_measure_is_aggregated_as_a_sum(self, content):
        config = v.viewer_default_config(content)
        for measure in v.viewer_measures(content).split():
            assert '"%s":"sum"' % measure in config

    def test_an_unknown_type_still_opens_on_something(self):
        """"library" is the one axis every content type has."""
        assert v.viewer_opening_axis("bogus") == "library"
        assert v.viewer_opening_columns("bogus") == "files sizeGigabytes"


class TestTheViewersOwnAxes:
    """The bitrate bands, the frame's shape and the codec's family and generation
    are axes the PAGE adds. Every axis added to a CUBE doubles its grouping sets -
    video's eleven are already 2048 of them - and the page rolls these up from the
    base grain at no such cost."""

    @pytest.mark.parametrize("content", TYPES)
    def test_they_are_all_in_the_dimension_list(self, content):
        dimensions = v.viewer_dimensions(content).split()
        for extra in (v.viewer_aspect_ratios(content),
                      v.viewer_codec_readings(content),
                      v.viewer_bitrate_tiers(content)):
            for axis in extra.split():
                assert axis in dimensions

    @pytest.mark.parametrize("content", TYPES)
    def test_and_none_of_them_is_a_cube_axis(self, content):
        cube_axes = set(cubes.DIMENSIONS[content])
        for extra in (v.viewer_aspect_ratios(content),
                      v.viewer_codec_readings(content),
                      v.viewer_bitrate_tiers(content)):
            for axis in extra.split():
                assert axis not in cube_axes

    def test_which_type_gets_which_axis(self):
        """The structural checks above hold just as well for a viewer that adds
        nothing at all, so what each type actually gets is named here."""
        assert v.viewer_aspect_ratios("video") == "aspectRatio"
        assert v.viewer_codec_readings("video") == \
            "videoCodecFamily videoCodecEra"
        # audio has one bitrate to band; video has its own and its first track's
        assert v.viewer_bitrate_tiers("audio") == "bitrateTier"
        assert v.viewer_bitrate_tiers("video") == \
            "videoBitrateTier firstAudioBitrateTier"

    @pytest.mark.parametrize("content", ["books", "comics"])
    def test_a_type_with_no_frame_and_no_stream_gets_none_of_them(self,
                                                                  content):
        assert v.viewer_aspect_ratios(content) == ""
        assert v.viewer_codec_readings(content) == ""
        assert v.viewer_bitrate_tiers(content) == ""

    @pytest.mark.parametrize("content", ["audio", "books", "comics"])
    def test_only_video_has_a_frame_to_have_a_shape(self, content):
        assert v.viewer_aspect_ratios(content) == ""
        assert v.viewer_codec_readings(content) == ""

    @pytest.mark.parametrize("content", TYPES)
    def test_the_cube_s_own_axes_come_first_and_all_of_them_do(self, content):
        dimensions = v.viewer_dimensions(content).split()
        assert dimensions[:len(cubes.DIMENSIONS[content])] == \
            list(cubes.DIMENSIONS[content])


class TestHtmlEscape:
    def test_the_ampersand_goes_first(self):
        """Or the ampersands the other three introduce are escaped again."""
        assert v.viewer_html_escape("<") == "&lt;"
        assert v.viewer_html_escape("&lt;") == "&amp;lt;"

    def test_all_four_characters(self):
        assert v.viewer_html_escape('a&<>"b') == "a&amp;&lt;&gt;&quot;b"

    def test_text_with_none_of_them_is_itself(self):
        assert v.viewer_html_escape("Ünïcødé 日本語") == "Ünïcødé 日本語"

    def test_the_empty_string(self):
        assert v.viewer_html_escape("") == ""


class TestBase64:
    def test_a_file_is_one_unwrapped_line(self, tmp_path):
        """Unwrapped because the result is dropped inside a JSON string in the
        page: a line break would end the string."""
        path = tmp_path / "long.csv"
        path.write_text("x" * 500, encoding="ascii")
        answer = v.viewer_base64(str(path))
        assert "\n" not in answer
        assert len(answer) > 500

    def test_an_empty_file_is_the_empty_line(self, tmp_path):
        path = tmp_path / "empty.csv"
        path.write_bytes(b"")
        assert v.viewer_base64(str(path)) == ""

    def test_and_a_file_that_is_not_there_is_no_answer_at_all(self, tmp_path):
        """Told apart from the empty file by the status, which is the only thing
        that distinguishes them - both base64 to nothing."""
        assert v.viewer_base64(str(tmp_path / "absent.csv")) is None

    def test_the_bytes_are_carried_exactly(self, tmp_path):
        """base64 and not a script block holding the CSV, because a path may hold
        any byte a filesystem allows - including the characters that would end
        the block early or be swallowed as an HTML entity."""
        import base64 as stdlib_base64
        payload = "a,b\n</script>&amp;\nÜnïcødé,日本語\n".encode()
        path = tmp_path / "awkward.csv"
        path.write_bytes(payload)
        assert stdlib_base64.b64decode(v.viewer_base64(str(path))) == payload


class TestThePage:
    def _page(self, tmp_path, title="Library", pairs=("video",)):
        specs = []
        for index, content in enumerate(pairs):
            csv = tmp_path / ("csv%d" % index)
            csv.write_text("library,files\nfilms,3\n", encoding="ascii")
            specs.append(content + ":" + str(csv))
        return v.viewer_html(title, specs)

    def test_it_is_a_whole_self_contained_document(self, tmp_path):
        page = self._page(tmp_path)
        assert page.startswith("<!DOCTYPE html>")
        assert page.rstrip().endswith("</html>")

    def test_the_title_is_escaped_in_both_places_it_appears(self, tmp_path):
        page = self._page(tmp_path, title="<Films> & Co")
        assert "<title>&lt;Films&gt; &amp; Co</title>" in page
        assert "<h1>&lt;Films&gt; &amp; Co</h1>" in page
        assert "<Films>" not in page

    def test_one_tab_per_type_in_the_order_given(self, tmp_path):
        page = self._page(tmp_path, pairs=("books", "video", "audio"))
        positions = [page.index('"%s": {' % content)
                     for content in ("books", "video", "audio")]
        assert positions == sorted(positions)

    def test_each_tab_carries_its_own_schema_and_config(self, tmp_path):
        page = self._page(tmp_path, pairs=("video", "books"))
        assert v.viewer_schema("video") in page
        assert v.viewer_schema("books") in page
        assert v.viewer_default_config("books") in page

    def test_the_data_is_embedded_and_never_fetched(self, tmp_path):
        page = self._page(tmp_path)
        assert '"csv": "' in page
        # the engine is the only thing loaded from the network
        for line in page.split("\n"):
            if "http" in line:
                assert v.CDN_BASE in line

    def test_a_page_with_no_tabs_is_still_a_page(self, tmp_path):
        page = v.viewer_html("Empty", [])
        assert page.startswith("<!DOCTYPE html>")
        assert "const DATA = {\n};" in page

    def test_the_engine_version_is_pinned_to_an_exact_release(self, tmp_path):
        """A major alone lets jsDelivr resolve the rest, and a resolved tag
        cannot be hashed."""
        page = self._page(tmp_path)
        assert "@perspective-dev/viewer@%s/" % v.PERSPECTIVE_VERSION in page
        assert re.fullmatch(r"\d+\.\d+\.\d+", v.PERSPECTIVE_VERSION), \
            "a floating tag cannot be hashed"

    def test_the_stylesheet_carries_its_hash(self, tmp_path):
        page = self._page(tmp_path)
        assert 'integrity="%s"' % v.PERSPECTIVE_SRI[v.PERSPECTIVE_STYLESHEET] \
            in page

    def test_the_scripts_carry_none_and_that_is_deliberate(self, tmp_path):
        """An `import` has nowhere to carry an integrity, and giving it one
        through a modulepreload makes the browser load Perspective twice over so
        the grid never renders. The version pin covers them."""
        page = self._page(tmp_path)
        assert page.count("integrity=") == 1
        assert "modulepreload" not in page

    def test_the_caller_can_name_another_base_and_version(self, tmp_path):
        csv = tmp_path / "c.csv"
        csv.write_text("a\n", encoding="ascii")
        page = v.viewer_html("T", ["video:" + str(csv)],
                             base="/vendor", version="7")
        assert "/vendor/@perspective-dev/viewer@7/" in page
        assert v.CDN_BASE not in page

    def test_another_build_gets_no_hash_rather_than_a_wrong_one(self, tmp_path):
        """The hash is of one release from one CDN. A page carrying it for a
        different build would not load at all, and an override is somebody
        asking for a different build on purpose."""
        csv = tmp_path / "c.csv"
        csv.write_text("a\n", encoding="ascii")
        for base, version in (("/vendor", v.PERSPECTIVE_VERSION),
                              (v.CDN_BASE, "7"),
                              ("/vendor", "7")):
            page = v.viewer_html("T", ["video:" + str(csv)],
                                 base=base, version=version)
            assert "integrity=" not in page, (base, version)


class TestTheAssetUrls:
    def test_the_version_goes_between_the_package_and_the_path(self):
        assert v.asset_url("@perspective-dev/viewer/dist/css/themes.css") == \
            (v.CDN_BASE + "/@perspective-dev/viewer@" + v.PERSPECTIVE_VERSION
             + "/dist/css/themes.css")

    def test_every_hash_is_keyed_on_an_asset_the_page_asks_for(self, tmp_path):
        """A hash for a URL the page never fetches is a hash that pins
        nothing."""
        csv = tmp_path / "c.csv"
        csv.write_text("a\n", encoding="ascii")
        page = v.viewer_html("T", ["video:" + str(csv)])
        for asset in v.PERSPECTIVE_SRI:
            assert v.asset_url(asset) in page

    def test_a_tab_whose_csv_is_missing_is_an_empty_one(self, tmp_path):
        page = v.viewer_html("T", ["video:" + str(tmp_path / "absent.csv")])
        assert '"csv": "" }' in page


class TestExportSql:
    @pytest.mark.parametrize("content", TYPES)
    def test_it_copies_the_grain_out_as_csv_with_a_header(self, content):
        sql = v.viewer_grain_export_sql(content, "/tmp/x.csv")
        assert sql.startswith("COPY (SELECT")
        assert sql.endswith("(FORMAT CSV, HEADER);\n")
        assert v.viewer_grain_sql(content) in sql

    def test_the_destination_is_a_quoted_sql_string(self):
        sql = v.viewer_grain_export_sql("video", "/out/it's here.csv")
        assert "TO '/out/it''s here.csv'" in sql

    def test_an_unknown_type_exports_nothing(self):
        assert v.viewer_grain_export_sql("bogus", "/tmp/x.csv") is None


class TestJsonList:
    def test_the_words_become_an_array_of_strings(self):
        assert v.viewer_json_list(["a", "b"]) == '["a","b"]'

    def test_one_word(self):
        assert v.viewer_json_list(["a"]) == '["a"]'

    def test_no_words_is_an_empty_array(self):
        assert v.viewer_json_list([]) == "[]"


class TestTitles:
    @pytest.mark.parametrize("content,expected", [
        ("audio", "Audio"), ("video", "Video"), ("books", "Books"),
        ("comics", "Comics")])
    def test_each_type_has_its_own_label(self, content, expected):
        assert v.viewer_title(content) == expected

    def test_a_type_this_module_does_not_know_is_its_own_label(self):
        assert v.viewer_title("bogus") == "bogus"
        assert v.viewer_title("") == ""
