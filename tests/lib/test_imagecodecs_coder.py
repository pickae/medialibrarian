"""The coder an ImageMagick input operand is read with.

ImageMagick left to itself picks the decoder from the file's content, so a page
called 001.jpg that holds SVG or MVG is read by a vector or drawing coder. What
is pinned here is the prefix that stops that: the coder the file's bytes name
when they are a raster format, else the one its extension implies, and nothing
at all when neither says.
"""

import pytest

from medialib.lib import imagecodecs

pytestmark = pytest.mark.fs

_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="5" height="5"/>'
_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
_WEBP = b"RIFF\x10\0\0\0WEBPVP8 " + b"\0" * 8
_AVIF = (b"\0\0\0\x20ftypmif1\0\0\0\0mif1avifmiaf" + b"\0" * 8)


class TestTheCoderAnInputIsReadWith:
    def test_svg_under_a_jpeg_name_is_read_as_a_jpeg(self, tmp_path):
        page = tmp_path / "001.jpg"
        page.write_bytes(_SVG)
        assert imagecodecs.coder_prefixed(str(page)) == "jpeg:%s" % page

    def test_a_png_under_a_jpeg_name_is_still_read_as_the_png_it_is(
            self, tmp_path):
        page = tmp_path / "001.jpg"
        page.write_bytes(_PNG)
        assert imagecodecs.coder_prefixed(str(page)) == "png:%s" % page

    def test_a_webp_sidecar_copied_under_a_jpeg_name(self, tmp_path):
        cover = tmp_path / "tempCover.jpg"
        cover.write_bytes(_WEBP)
        assert imagecodecs.coder_prefixed(str(cover)) == "webp:%s" % cover

    def test_an_avif_naming_itself_after_the_generic_brand(self, tmp_path):
        page = tmp_path / "page.heic"
        page.write_bytes(_AVIF)
        assert imagecodecs.magick_coder(str(page)) == "avif"

    @pytest.mark.parametrize("name, coder", [
        ("page.JPG", "jpeg"), ("page.jpeg", "jpeg"), ("page.png", "png"),
        ("page.tif", "tiff"), ("page.webp", "webp"), ("page.bmp", "bmp"),
        ("page.jp2", "jp2"), ("page.pgm", "pnm"), ("page.gif", "gif")])
    def test_without_bytes_to_go_by_the_extension_decides(self, name, coder):
        assert imagecodecs.coder_prefixed("absent/" + name) == \
            "%s:absent/%s" % (coder, name)

    @pytest.mark.parametrize("operand", [
        "-", "", "drawing.svg", "notes.txt", "no-extension",
        "jpeg:page.jpg", "miff:/tmp/trimmed_1.miff", "PNG:page.png"])
    def test_what_cannot_be_told_is_left_as_it_is(self, operand):
        assert imagecodecs.coder_prefixed(operand) == operand

    def test_a_frame_selector_stays_at_the_end(self, tmp_path):
        anim = tmp_path / "anim.gif"
        anim.write_bytes(b"GIF89a" + b"\0" * 10)
        assert imagecodecs.coder_prefixed("%s[0]" % anim) == "gif:%s[0]" % anim

    def test_a_dot_slash_path_keeps_its_spelling(self):
        assert imagecodecs.coder_prefixed("./sub/page.png") == \
            "png:./sub/page.png"

    def test_a_file_whose_name_looks_like_a_coder_is_still_prefixed(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "png:odd.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        assert imagecodecs.coder_prefixed("png:odd.jpg") == "jpeg:png:odd.jpg"
