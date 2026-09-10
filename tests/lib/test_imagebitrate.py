"""The white box for medialib/lib/imagebitrate.py.

Two halves. The arithmetic is pure - the anchor and its three axes, and which way
each one moves the requirement - and is pinned as relations rather than as
figures wherever a figure would only re-state the table: what has to hold is that
a better codec asks for less, a bigger picture asks for less per pixel, and an
unmeasured axis can only be kind.

The probes are the other half, and they are read from the text ``identify``
prints rather than from a picture on disk, which is what lets the reading be
pinned without an encoder in the loop.
"""

import pytest

from medialib.lib import adequacy
from medialib.lib import imagebitrate as ib

pytestmark = pytest.mark.pure


def _bpp(codec, width, height, colour="", detail=""):
    """The requirement as bits per pixel, which is the shape it is reasoned in."""
    return float(ib.adequate_image_bytes(codec, width, height, colour,
                                         detail)) * 8 / (width * height)


class TestTheAnchor:
    def test_a_one_megapixel_colour_photographic_jpeg_is_the_reference(self):
        """One bit per pixel, exactly - every other figure this module produces
        is this one scaled, so it is the one worth stating outright."""
        assert _bpp("jpeg", 1000, 1000, ib.COLOUR, ib.PHOTO) == \
            pytest.approx(ib.adequate_bpp_1mp)

    def test_and_generous_is_the_median_jpeg_the_web_serves(self):
        """2 bpp, which is what the HTTP Archive measures the median web JPEG at
        - the file that can be halved and still look the same, which is the
        reading this whole model exists to make."""
        adequate = float(ib.adequate_image_bytes("jpeg", 1000, 1000, ib.COLOUR,
                                                 ib.PHOTO))
        assert ib.image_verdict(adequate * adequacy.GENEROUS_FACTOR, adequate,
                                "jpeg") == adequacy.GENEROUS


class TestTheCodecAxis:
    @pytest.mark.parametrize("better,worse", [
        ("webp", "jpeg"), ("avif", "webp"), ("jxl", "avif"),
        ("heic", "webp"), ("jpeg2000", "jpeg"),
    ])
    def test_a_more_efficient_format_needs_fewer_bytes(self, better, worse):
        assert _bpp(better, 2000, 1500) < _bpp(worse, 2000, 1500)

    @pytest.mark.parametrize("lossless", ["png", "tiff", "bmp", "gif"])
    def test_and_a_format_that_keeps_every_pixel_needs_more_than_any_of_them(
            self, lossless):
        assert _bpp(lossless, 2000, 1500) > _bpp("jpeg", 2000, 1500)

    def test_a_format_nobody_could_identify_is_tuned_as_jpeg(self):
        """The ladder's reference point, so an unidentified file is judged
        neither generously nor harshly."""
        assert ib.adequate_image_bytes("", 2000, 1500) \
            == ib.adequate_image_bytes("jpeg", 2000, 1500)

    def test_and_is_named_as_the_unknown_it_is(self):
        factor, exponent, kind, family = ib.image_codec_tuning("xcf").split()
        assert (factor, exponent) == ("1.00", "0.95")
        assert kind == family == "unknown"


class TestThePixelAxis:
    def test_a_bigger_picture_needs_more_bytes(self):
        small = float(ib.adequate_image_bytes("jpeg", 1000, 1000))
        large = float(ib.adequate_image_bytes("jpeg", 4000, 4000))
        assert large > small

    def test_but_fewer_bytes_per_pixel(self):
        """A codec with big adaptive blocks finds more to predict in a larger
        frame. Sub-linearly and only just: a still has no temporal prediction
        whose gains grow with the frame."""
        assert _bpp("jpeg", 4000, 4000) < _bpp("jpeg", 1000, 1000)

    def test_a_format_with_no_prediction_at_all_scales_exactly_linearly(self):
        """An uncompressed BMP costs its pixel count and nothing else, so its
        requirement per pixel is the same at every size."""
        assert _bpp("bmp", 500, 500) == pytest.approx(_bpp("bmp", 5000, 5000))

    def test_a_modern_format_gains_more_from_the_size_than_jpeg_does(self):
        def drop(codec):
            return _bpp(codec, 4000, 4000) / _bpp(codec, 500, 500)
        assert drop("avif") < drop("jpeg")

    @pytest.mark.parametrize("width,height", [
        (0, 1000), (1000, 0), ("", "1000"), ("abc", "1000"), (-4, 1000)])
    def test_a_size_that_could_not_be_read_states_nothing(self, width, height):
        """Every axis is a multiple of the pixel count, so a guessed size would
        be a guessed verdict."""
        assert ib.adequate_image_bytes("jpeg", width, height) == ""


class TestTheContentAxes:
    def test_a_greyscale_picture_needs_less_than_a_colour_one(self):
        assert _bpp("jpeg", 2000, 1500, ib.GREY) < \
            _bpp("jpeg", 2000, 1500, ib.COLOUR)

    def test_flat_artwork_needs_less_than_a_photograph_and_line_art_less_again(
            self):
        photo = _bpp("jpeg", 2000, 1500, ib.COLOUR, ib.PHOTO)
        art = _bpp("jpeg", 2000, 1500, ib.COLOUR, ib.ART)
        line = _bpp("jpeg", 2000, 1500, ib.COLOUR, ib.LINE)
        assert line < art < photo

    def test_an_unmeasured_detail_axis_is_read_as_flat_artwork(self):
        """The reading that cannot manufacture a starved verdict: it understates
        what a photograph needs, which can only make a verdict kinder."""
        assert ib.image_detail_factor("") == \
            ib.image_detail_factor(ib.UNMEASURED_DETAIL)
        assert float(ib.image_detail_factor("")) < \
            float(ib.image_detail_factor(ib.PHOTO))

    def test_and_so_is_a_word_the_table_has_never_heard_of(self):
        assert ib.image_detail_factor("cartoon") == ib.image_detail_factor("")

    def test_an_unmeasured_colour_axis_is_read_as_colour_instead(self):
        """The other way round, because this axis is never really unmeasured: a
        header states it, so a blank means the probe failed - and reading a
        colour picture as grey would judge it against four fifths of what it
        needs."""
        assert ib.image_colour_factor("") == ib.image_colour_factor(ib.COLOUR)


class TestTheVerdict:
    def test_a_lossy_file_below_its_requirement_is_starved(self):
        adequate = ib.adequate_image_bytes("jpeg", 2000, 1500, ib.COLOUR,
                                           ib.PHOTO)
        assert ib.image_verdict(float(adequate) / 2, adequate, "jpeg") \
            == adequacy.STARVED

    @pytest.mark.parametrize("codec", ["png", "tiff", "bmp", "gif", "ico"])
    def test_but_a_lossless_or_palette_one_never_is(self, codec):
        """It holds every pixel it was given, so "it was not given enough bytes"
        is not a thing that can be true of it - a 400-byte lossless icon is a
        small picture, not a degraded one."""
        adequate = ib.adequate_image_bytes(codec, 2000, 1500)
        assert ib.image_verdict(1, adequate, codec) == adequacy.ADEQUATE

    def test_and_a_large_lossless_one_is_still_generous(self):
        """The floor lifts starved and leaves the reading that still means
        something - whether re-encoding would save anything - alone."""
        adequate = ib.adequate_image_bytes("png", 2000, 1500)
        assert ib.image_verdict(float(adequate) * 4, adequate, "png") \
            == adequacy.GENEROUS

    def test_a_vector_drawing_has_no_verdict_at_all(self):
        """Its size is what its shapes cost and has nothing to do with the pixels
        something rasterises it into, so neither half of the reading means
        anything - and unknown is what every caller here converts."""
        assert ib.image_verdict(10, "100000", "svg") == adequacy.UNKNOWN
        assert ib.image_verdict(10 ** 9, "100000", "svg") == adequacy.UNKNOWN

    def test_a_format_nothing_could_identify_is_judged_as_the_lossy_one_it_may_be(
            self):
        """The alternative is to declare every unreadable file safe to
        re-encode."""
        assert ib.image_verdict(1, "100000", "") == adequacy.STARVED


class TestReadingTheCheapProbe:
    @pytest.mark.parametrize("answer,expected", [
        ("JPEG|1200|1600|sRGB", "jpeg 1200 1600 colour"),
        ("PNG|800|600|Gray", "png 800 600 grey"),
        ("PNG|800|600|gray", "png 800 600 grey"),
        # the format is kept as the probe spelled it, lower-cased: the family
        # lookup is a separate reading of it
        ("MPO|4000|3000|sRGB", "mpo 4000 3000 colour"),
    ])
    def test_the_four_fields(self, answer, expected):
        assert ib.stats_from_identify(answer) == expected

    @pytest.mark.parametrize("answer", [
        "", "JPEG|1200|1600", "JPEG||1600|sRGB", "JPEG|12.5|1600|sRGB",
        "JPEG|abc|1600|sRGB", "JPEG|1200|-40|sRGB"])
    def test_and_nothing_at_all_when_the_size_is_not_two_whole_numbers(
            self, answer):
        assert ib.stats_from_identify(answer) == ""

    def test_a_colourspace_it_could_not_read_is_colour(self):
        assert ib.stats_from_identify("JPEG|10|10|").endswith("colour")


class TestReadingTheDeepProbe:
    @pytest.mark.parametrize("answer,expected", [
        ("Bilevel|2", ib.LINE),
        ("Grayscale|4", ib.LINE),
        ("Grayscale|900", ib.ART),
        ("Palette|200", ib.ART),
        ("PaletteAlpha|200", ib.ART),
        ("TrueColor|900", ib.ART),
        ("TrueColor|60000", ib.PHOTO),
        # a palette stays flat artwork however many entries it has
        ("Palette|60000", ib.ART),
    ])
    def test_the_type_and_the_colour_count_together(self, answer, expected):
        assert ib.detail_from_identify(answer) == expected

    def test_the_boundaries(self):
        assert ib.detail_from_identify(
            "TrueColor|%d" % ib.LINE_COLOUR_CEILING) == ib.LINE
        assert ib.detail_from_identify(
            "TrueColor|%d" % (ib.LINE_COLOUR_CEILING + 1)) == ib.ART
        assert ib.detail_from_identify(
            "TrueColor|%d" % ib.ART_COLOUR_CEILING) == ib.ART
        assert ib.detail_from_identify(
            "TrueColor|%d" % (ib.ART_COLOUR_CEILING + 1)) == ib.PHOTO

    @pytest.mark.parametrize("answer", ["", "TrueColor", "TrueColor|",
                                        "TrueColor|many"])
    def test_a_count_nothing_produced_leaves_the_axis_unmeasured(self, answer):
        """Only the type is left to go on, and TrueColor on its own says nothing
        - a comic page saved as a JPEG is TrueColor too."""
        assert ib.detail_from_identify(answer) == ""

    def test_except_for_a_palette_which_is_flat_by_construction(self):
        assert ib.detail_from_identify("Palette|") == ib.ART
