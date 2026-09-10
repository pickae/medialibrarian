"""The white box for medialib/lib/imagecodecs.py.

The one thing here that is a contract rather than a table lookup is the agreement
with ``enums.IMAGE_EXTENSIONS``: the suffixes a command filters an input tree on
and the suffixes this table knows what to do with have to be the same set, and
they live in two files because neither can import the other.
"""

import pytest

from medialib.lib import enums, imagecodecs

pytestmark = pytest.mark.pure


class TestTheTableAgreesWithTheInputList:
    def test_every_raster_suffix_is_an_input_extension_and_the_other_way_round(
            self):
        """The check the comment in enums.py points at. A suffix in one and not
        the other is either a file a command reads and cannot judge, or a format
        the model has a figure for and nothing ever hands it."""
        assert set(imagecodecs.raster_extensions()) \
            == set(enums.IMAGE_EXTENSIONS)

    def test_the_vector_suffixes_are_deliberately_not_input_extensions(self):
        """An SVG has no resolution and no size worth judging."""
        assert "svg" not in enums.IMAGE_EXTENSIONS
        assert imagecodecs.kind_of("svg") == imagecodecs.VECTOR

    def test_no_suffix_is_claimed_by_two_families(self):
        seen = set()
        for family, _kind, extensions, _aliases in imagecodecs.TABLE:
            for extension in extensions:
                assert extension not in seen, "%s is in two rows" % extension
                seen.add(extension)
            assert family not in seen or family in extensions


class TestFamilyOf:
    @pytest.mark.parametrize("name,family", [
        ("jpg", "jpeg"), ("jpeg", "jpeg"), ("jfif", "jpeg"),
        ("JPG", "jpeg"), (".jpg", "jpeg"),
        # what ImageMagick's %m prints, and what ffprobe calls the same thing
        ("JPEG", "jpeg"), ("mjpeg", "jpeg"), ("MPO", "jpeg"),
        ("PNG", "png"), ("apng", "png"),
        ("tif", "tiff"), ("TIFF", "tiff"),
        ("heif", "heic"), ("hif", "heic"),
        ("jp2", "jpeg2000"), ("j2k", "jpeg2000"),
        ("avif", "avif"), ("jxl", "jxl"), ("webp", "webp"),
        ("gif", "gif"), ("ico", "ico"), ("cur", "ico"),
        ("pgm", "netpbm"), ("pbm", "netpbm"),
    ])
    def test_every_spelling_reaches_its_family(self, name, family):
        assert imagecodecs.family_of(name) == family

    @pytest.mark.parametrize("name", ["", "mp3", "not a format", "xcf"])
    def test_and_an_unlisted_one_stays_unknown(self, name):
        """Never a guess. A caller judging a file it could not identify has to be
        told that is what happened."""
        assert imagecodecs.family_of(name) == imagecodecs.UNKNOWN
        assert imagecodecs.kind_of(name) == imagecodecs.UNKNOWN


class TestKindOf:
    @pytest.mark.parametrize("name,kind", [
        ("jpeg", imagecodecs.LOSSY), ("webp", imagecodecs.LOSSY),
        ("avif", imagecodecs.LOSSY), ("jxl", imagecodecs.LOSSY),
        ("heic", imagecodecs.LOSSY), ("jp2", imagecodecs.LOSSY),
        ("png", imagecodecs.LOSSLESS), ("tiff", imagecodecs.LOSSLESS),
        ("bmp", imagecodecs.LOSSLESS), ("psd", imagecodecs.LOSSLESS),
        ("gif", imagecodecs.PALETTE), ("ico", imagecodecs.PALETTE),
        ("svg", imagecodecs.VECTOR),
    ])
    def test_each_format_is_what_it_is_used_as(self, name, kind):
        assert imagecodecs.kind_of(name) == kind


class TestTheOtherLookups:
    def test_extensions_of_leads_with_the_one_to_write(self):
        assert imagecodecs.extensions_of("jpeg")[0] == "jpg"
        assert imagecodecs.extensions_of("tiff")[0] == "tif"

    def test_and_answers_nothing_for_a_name_it_does_not_know(self):
        assert imagecodecs.extensions_of("xcf") == ()

    def test_aliases_of_answers_none_rather_than_an_empty_line(self):
        assert imagecodecs.aliases_of("xcf") is None
        assert imagecodecs.aliases_of("webp") == ""

    def test_families_are_the_lossy_ones_first(self):
        families = imagecodecs.families()
        kinds = [imagecodecs.kind_of(family) for family in families]
        assert kinds.index(imagecodecs.LOSSLESS) > kinds.index(
            imagecodecs.LOSSY)


class TestTheSql:
    def test_the_case_answers_what_the_lookup_answers(self):
        """Generated from the same table, so a report grouped in the database and
        a file judged in Python land in the same bucket."""
        for family, kind, extensions, aliases in imagecodecs.TABLE:
            for spelling in (family,) + extensions + aliases:
                assert "'%s'" % spelling.lower() in imagecodecs.family_sql("c")
                assert "THEN '%s'" % kind in imagecodecs.kind_sql("c")

    def test_an_empty_column_is_unknown_and_not_the_else(self):
        for sql in (imagecodecs.family_sql("c"), imagecodecs.kind_sql("c")):
            head = sql.split("WHEN", 2)[1]
            assert "IS NULL" in head and imagecodecs.UNKNOWN in head

    def test_a_column_neither_arm_matches_is_unknown_too(self):
        assert imagecodecs.family_sql("c").rstrip().endswith(
            "ELSE '%s'\n        END" % imagecodecs.UNKNOWN)

    def test_a_column_that_is_not_one_of_the_two_is_refused(self):
        with pytest.raises(ValueError):
            imagecodecs._case_sql("c", "extensions")
