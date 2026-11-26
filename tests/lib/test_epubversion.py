"""The white box for medialib/lib/epubversion.py.

Each case is a tiny epub written with zipfile: a package document and one page,
holding just the markup the case is about. Which version a book is written as
is decided from that markup alone, so no converter is involved.
"""

import zipfile

import pytest

from medialib.lib import epubversion

_PLAIN_PACKAGE = (
    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
    '<metadata/><manifest>'
    '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
    '</manifest><spine><itemref idref="c1"/></spine></package>')

_PLAIN_PAGE = ('<html><body><p>Words, an <svg/> and a '
               '<mathematics>tag</mathematics>.</p></body></html>')


def _epub(path, page=_PLAIN_PAGE, package=_PLAIN_PACKAGE, page_name="OEBPS/c1.xhtml"):
    with zipfile.ZipFile(path, "w") as packed:
        packed.writestr("mimetype", "application/epub+zip")
        packed.writestr("OEBPS/content.opf", package)
        packed.writestr(page_name, page)
    return str(path)


class TestNeedsEpub3:
    def test_a_plain_book_loses_nothing_as_epub2(self, tmp_path):
        """SVG is EPUB 2 too, and an element that merely starts with "math"
        is not MathML."""
        assert epubversion.needs_epub3(_epub(tmp_path / "b.epub")) == []

    @pytest.mark.parametrize("markup, named", [
        ('<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>', "MathML"),
        ('<m:math xmlns:m="http://www.w3.org/1998/Math/MathML"/>', "MathML"),
        ('<audio src="a.mp3" controls="controls"/>', "audio"),
        ('<video src="v.mp4">\n</video>', "video"),
    ])
    def test_content_only_epub3_carries_is_named(self, tmp_path, markup, named):
        book = _epub(tmp_path / "b.epub", "<html><body>%s</body></html>" % markup)
        assert epubversion.needs_epub3(book) == [named]

    def test_everything_found_is_named_once_in_a_fixed_order(self, tmp_path):
        page = "<video/><math/><audio/><math/>"
        book = _epub(tmp_path / "b.epub", page)
        assert epubversion.needs_epub3(book) == ["MathML", "audio", "video"]

    @pytest.mark.parametrize("name", ["OEBPS/c1.html", "OEBPS/C1.HTM"])
    def test_every_page_suffix_is_read(self, tmp_path, name):
        book = _epub(tmp_path / "b.epub", "<math/>", page_name=name)
        assert epubversion.needs_epub3(book) == ["MathML"]

    def test_markup_outside_the_pages_is_not_read(self, tmp_path):
        """A stylesheet or script naming the element is not the element."""
        book = _epub(tmp_path / "b.epub", "<math/>", page_name="OEBPS/style.css")
        assert epubversion.needs_epub3(book) == []

    def test_a_file_that_is_not_a_zip_asks_for_nothing(self, tmp_path):
        book = tmp_path / "b.epub"
        book.write_text("<math/>")
        assert epubversion.needs_epub3(str(book)) == []

    def test_a_missing_file_asks_for_nothing(self, tmp_path):
        assert epubversion.needs_epub3(str(tmp_path / "gone.epub")) == []


class TestCalibreDrops:
    def test_a_plain_book_loses_nothing(self, tmp_path):
        assert epubversion.calibre_drops(_epub(tmp_path / "b.epub")) == []

    def test_a_fixed_layout_is_named(self, tmp_path):
        package = _PLAIN_PACKAGE.replace(
            "<metadata/>",
            '<metadata><meta property="rendition:layout">pre-paginated</meta></metadata>')
        book = _epub(tmp_path / "b.epub", package=package)
        assert epubversion.calibre_drops(book) == ["fixed layout"]

    def test_a_reflowable_layout_is_not(self, tmp_path):
        package = _PLAIN_PACKAGE.replace(
            "<metadata/>",
            '<metadata><meta property="rendition:layout">reflowable</meta></metadata>')
        assert epubversion.calibre_drops(_epub(tmp_path / "b.epub", package=package)) == []

    def test_a_fixed_layout_set_on_one_page_is_named(self, tmp_path):
        package = _PLAIN_PACKAGE.replace(
            '<itemref idref="c1"/>',
            '<itemref idref="c1" properties="rendition:layout-pre-paginated"/>')
        book = _epub(tmp_path / "b.epub", package=package)
        assert epubversion.calibre_drops(book) == ["fixed layout"]

    def test_a_media_overlay_is_named(self, tmp_path):
        package = _PLAIN_PACKAGE.replace(
            'media-type="application/xhtml+xml"/>',
            'media-type="application/xhtml+xml" media-overlay="mo1"/>')
        book = _epub(tmp_path / "b.epub", package=package)
        assert epubversion.calibre_drops(book) == ["read-aloud"]

    def test_a_file_that_is_not_a_zip_drops_nothing(self, tmp_path):
        book = tmp_path / "b.epub"
        book.write_text("x")
        assert epubversion.calibre_drops(str(book)) == []
