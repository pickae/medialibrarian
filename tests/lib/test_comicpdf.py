"""The white box for medialib/lib/comicpdf.py.

The poppler tools are the shared tool stub with its printing and page-writing
extensions, so "which tool was reached for, with what" and "what the tool said"
are both under the test's control. What is pinned is the judgement itself - the
arithmetic the shell ran the listings through awk for - at the boundaries it
draws.
"""

import os
import shutil
from types import SimpleNamespace

import pytest

from medialib.lib import comicpdf
from tests import blackbox

pytestmark = pytest.mark.stubbed

_TOOLSTUB = blackbox.TOOLSTUB

_IMAGES_HEADER = ("page   num  type   width height  color comp bpc  enc    "
                  "interp  object ID x-ppi y-ppi size ratio")


def _info(pages, size=(612, 792), without=(), pages_line="Pages:    {}"):
    lines = [pages_line.format(pages)]
    for n in range(1, pages + 1):
        if n not in without:
            lines.append(f"Page {n:>4} size:  {size[0]} x {size[1]} pts (letter)")
    return "\n".join(lines) + "\n"


def _img(page, w="2550", h="3300", xppi="300", yppi="300", typ="image", old=False):
    line = f"   {page}     0 {typ}    {w}  {h}  rgb   3   8  /DCT false 4  0"
    if not old:
        line += f"   {xppi}   {yppi}   12  1.0"
    return line


def _images(*rows):
    return "\n".join([_IMAGES_HEADER, *rows]) + "\n"


# A clean full scan: 2550 by 3300 at 300 ppi on 612 by 792 points is the page
# area to the pixel, and it works out to exactly 100 percent in the arithmetic
# the judgement does.
_FULL = "2550 3300"
# Half the page AREA: 2051 squared is 49.99 percent of 612 by 792 at 300 ppi.
_HALF = "2051 2051"


@pytest.fixture()
def comic(tmp_path, monkeypatch):
    """A PATH holding only the named stubs, and the fixtures they will print.

    The stubs are copies of the shared tool stub: they record the call
    (their name, then each argument, tab-separated) and print what the test
    hands them, from TOOLSTUB_OUT/<tool> when that file exists.
    """
    bin_dir = tmp_path / "bin"
    out_dir = tmp_path / "out"
    state_dir = tmp_path / "state"
    bin_dir.mkdir()
    out_dir.mkdir()
    state_dir.mkdir()
    (bin_dir / "bash").symlink_to(shutil.which("bash"))
    (bin_dir / "cat").symlink_to(shutil.which("cat"))
    record = tmp_path / "calls"
    installed = []

    def install(name):
        shutil.copyfile(_TOOLSTUB, str(bin_dir / name))
        os.chmod(str(bin_dir / name), 0o755)
        installed.append(name)

    def say(name, text):
        (out_dir / name).write_text(text)

    def calls():
        if not record.exists():
            return []
        return [line.rstrip("\n").split("\t")[1:]
                for line in record.read_text().splitlines() if line]

    def clear():
        if record.exists():
            record.unlink()

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("TOOLSTUB_LOG", str(record))
    monkeypatch.setenv("TOOLSTUB_OUT", str(out_dir))
    monkeypatch.setenv("TOOLSTUB_STATE", str(state_dir))
    monkeypatch.setenv("TOOLSTUB_TOUCH", str(tmp_path / "pages" / "page-01.jpg"))
    monkeypatch.setenv("TOOLSTUB_SKIP", "0")
    return SimpleNamespace(install=install, say=say, calls=calls, clear=clear,
                           bin_dir=bin_dir, tmp_path=tmp_path)


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    """The judgement's knobs start at the module's own defaults."""
    for knob in ("comicPdfMinCoverage", "comicPdfMinPixels",
                 "comicPdfMinPageShare", "comicPdfMinDpi", "comicPdfMaxDpi",
                 "comicPdfFallbackDpi", "comicPdfJpegQuality"):
        monkeypatch.delenv(knob, raising=False)


def _expect_calls(c, *argvs):
    got = [(call[0], call[1:]) for call in c.calls()]
    assert got == [(tool, args) for tool, args in argvs]


def test_the_stats_line_of_a_clean_comic(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(10))
    comic.say("pdfimages", _images(*[_img(n) for n in range(1, 9)]))
    status = comicpdf.comic_pdf_stats("/book.pdf")
    assert capsys.readouterr().out == "10 8 300\n"
    assert status == 0
    _expect_calls(comic,
                  ("pdfinfo", ["/book.pdf"]),
                  ("pdfinfo", ["-f", "1", "-l", "10", "/book.pdf"]),
                  ("pdfimages", ["-list", "-f", "1", "-l", "10", "/book.pdf"]))


@pytest.mark.parametrize("info", [
    "".join(_info(5).splitlines(keepends=True)[1:]),   # no Pages line at all
    _info(5).replace("Pages:    5", "Pages:    0"),
    _info(5).replace("Pages:    5", "Pages:    twelve"),
])
def test_a_page_count_it_cannot_read(capsys, comic, info):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", info)
    comic.say("pdfimages", _images(*[_img(n) for n in range(1, 6)]))
    status = comicpdf.comic_pdf_stats("/book.pdf")
    assert capsys.readouterr().out == "0 0 0\n"
    assert status == 1
    assert len(comic.calls()) == 1


def test_without_pdfinfo(capsys, comic):
    comic.install("pdfimages")
    comic.say("pdfimages", _images(*[_img(n) for n in range(1, 4)]))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 1
    assert capsys.readouterr().out == "0 0 0\n"


def test_without_pdfimages_no_page_is_a_full_page_image(capsys, comic):
    comic.install("pdfinfo")
    comic.say("pdfinfo", _info(3))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "3 0 300\n"
    _expect_calls(comic,
                  ("pdfinfo", ["/book.pdf"]),
                  ("pdfinfo", ["-f", "1", "-l", "3", "/book.pdf"]))


def test_an_image_short_of_the_coverage(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1))
    comic.say("pdfimages", _images(_img(1, *_HALF.split())))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 0 300\n"


def test_an_image_at_the_coverage_counts(capsys, comic, monkeypatch):
    # 72 by 144 at 72 ppi on a 144 by 144 page: half the area, to the bit.
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1, size=(144, 144)))
    comic.say("pdfimages", _images(_img(1, 72, 144, "72", "72")))
    monkeypatch.setenv("comicPdfMinCoverage", "50")
    monkeypatch.setenv("comicPdfMinPixels", "50")
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 1 150\n"


def test_a_sideways_image_counts_by_area(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1))
    comic.say("pdfimages", _images(_img(1, 3300, 2550)))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 1 300\n"


def test_zero_ppi_falls_back_to_the_pixel_floor(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1))
    comic.say("pdfimages", _images(_img(1, *_FULL.split(), "0", "0")))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 1 300\n"


def test_an_old_listing_without_ppi(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1))
    comic.say("pdfimages", _images(_img(1, *_FULL.split(), old=True)))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 1 300\n"


def test_an_image_below_the_pixel_floor_is_decoration(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1))
    comic.say("pdfimages", _images(_img(1, 100, 120)))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 0 300\n"


def test_a_stencil_is_the_page_s_image(capsys, comic):
    """A bilevel scan - line art, or an old black-and-white book - is listed as
    a "stencil" rather than an "image", and it is still the page."""
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(2))
    comic.say("pdfimages", _images(_img(1, *_FULL.split(), typ="stencil"),
                                   _img(2, *_FULL.split(), typ="stencil")))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "2 2 300\n"


def test_an_smask_is_not_a_second_image(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1))
    comic.say("pdfimages", _images(_img(1, *_FULL.split()),
                                   _img(1, *_FULL.split(), typ="smask")))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 1 300\n"


def test_a_form_object_is_not_an_image(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1))
    comic.say("pdfimages", _images(_img(1, *_FULL.split(), typ="form")))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 0 300\n"


def test_two_images_is_a_layout(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1))
    comic.say("pdfimages", _images(_img(1, *_HALF.split()),
                                   _img(1, *_HALF.split())))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 0 300\n"


def test_a_page_with_no_size_is_judged_by_pixels(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1, without=(1,)))
    comic.say("pdfimages", _images(_img(1, *_FULL.split())))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 1 300\n"


def test_the_dpi_comes_from_the_good_pages_only(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(2))
    comic.say("pdfimages", _images(_img(1, *_FULL.split()),
                                   _img(2, *_HALF.split(), "1200", "1200")))
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "2 1 300\n"


def test_the_dpi_is_clamped(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1))
    for ppi, expected in (("72", "150"),   # below the floor
                          ("300", "300"),  # in its own band
                          ("1200", "600")):  # past the ceiling
        # The page's own pixels at that ppi, so the coverage stays 100.
        w, h = round(612 / 72 * int(ppi)), round(792 / 72 * int(ppi))
        comic.say("pdfimages", _images(_img(1, w, h, ppi, ppi)))
        comic.clear()
        assert comicpdf.comic_pdf_stats("/book.pdf") == 0
        assert capsys.readouterr().out == f"1 1 {expected}\n"


def test_the_dpi_falls_back_when_no_page_is_good(capsys, comic, monkeypatch):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1))
    comic.say("pdfimages", _images(_img(1, *_HALF.split())))
    monkeypatch.setenv("comicPdfFallbackDpi", "450")
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 0 450\n"


def test_the_height_cap(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1))
    comic.say("pdfimages", _images(_img(1, *_FULL.split())))
    assert comicpdf.comic_pdf_stats("/book.pdf", "2000") == 0
    assert capsys.readouterr().out == "1 1 182\n"
    comic.clear()
    assert comicpdf.comic_pdf_stats("/book.pdf", "2959") == 0
    assert capsys.readouterr().out == "1 1 269\n"


def test_an_asymmetric_ppi_takes_the_higher(capsys, comic, monkeypatch):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1))
    # Half the page by area, but the pair's higher column is the dpi.
    comic.say("pdfimages", _images(_img(1, *_FULL.split(), "300", "600")))
    monkeypatch.setenv("comicPdfMinCoverage", "50")
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 1 600\n"


@pytest.mark.parametrize("good,share,expected", [
    (8, "90", 1), (8, "80", 0), (8, "50", 0), (0, "50", 1),
])
def test_the_verdict(capsys, comic, good, share, expected, monkeypatch):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(10))
    comic.say("pdfimages", _images(*[_img(n) for n in range(1, good + 1)]))
    monkeypatch.setenv("comicPdfMinPageShare", share)
    status = comicpdf.is_comic_pdf("/book.pdf")
    assert capsys.readouterr().out == f"10 {good} 300\n"
    assert status == expected


def test_the_verdict_on_a_file_it_cannot_read(capsys, comic, monkeypatch):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(5).replace("Pages:    5", "Pages:    0"))
    comic.say("pdfimages", _images())
    assert comicpdf.is_comic_pdf("/book.pdf") == 1
    assert capsys.readouterr().out == "0 0 0\n"


def test_rendering_a_comic(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.install("pdftoppm")
    comic.say("pdfinfo", _info(10))
    comic.say("pdfimages", _images(*[_img(n) for n in range(1, 9)]))
    dest = str(comic.tmp_path / "pages")
    assert comicpdf.render_comic_pdf_pages("/book.pdf", dest) == 0
    _expect_calls(comic,
                  ("pdfinfo", ["/book.pdf"]),
                  ("pdfinfo", ["-f", "1", "-l", "10", "/book.pdf"]),
                  ("pdfimages", ["-list", "-f", "1", "-l", "10", "/book.pdf"]),
                  ("pdftoppm", ["-jpeg", "-jpegopt", "quality=92",
                                "-r", "300", "/book.pdf", dest + "/page"]))
    assert (comic.tmp_path / "pages" / "page-01.jpg").is_file()


def test_a_render_whose_first_pass_comes_up_empty(capsys, comic, monkeypatch):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.install("pdftoppm")
    comic.say("pdfinfo", _info(2))
    comic.say("pdfimages", _images(_img(1, *_FULL.split())))
    monkeypatch.setenv("TOOLSTUB_SKIP", "1")
    dest = str(comic.tmp_path / "pages")
    assert comicpdf.render_comic_pdf_pages("/book.pdf", dest) == 0
    toppm = [c for c in comic.calls() if c[0] == "pdftoppm"]
    assert toppm[0][1:] == ["-jpeg", "-jpegopt", "quality=92", "-r", "300",
                            "/book.pdf", dest + "/page"]
    assert toppm[1][1:] == ["-jpeg", "-r", "300", "/book.pdf", dest + "/page"]


def test_a_render_that_yields_no_page(capsys, comic, monkeypatch):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.install("pdftoppm")
    comic.say("pdfinfo", _info(2))
    comic.say("pdfimages", _images())
    monkeypatch.setenv("TOOLSTUB_SKIP", "9")
    assert comicpdf.render_comic_pdf_pages("/book.pdf",
                                           str(comic.tmp_path / "pages")) == 1
    comic.clear()
    assert not (comic.tmp_path / "pages" / "page-01.jpg").exists()


def test_a_render_without_pdftoppm(capsys, comic):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(2))
    comic.say("pdfimages", _images(_img(1, *_FULL.split())))
    assert comicpdf.render_comic_pdf_pages("/book.pdf",
                                           str(comic.tmp_path / "pages")) == 1
    assert [c[0] for c in comic.calls()] == ["pdfinfo", "pdfinfo", "pdfimages"]


@pytest.mark.parametrize("have, expected", [
    ((True, True, True), ""),
    ((False, False, False), "pdfinfo pdfimages pdftoppm"),
    ((False, True, False), "pdfinfo pdftoppm"),
    ((True, False, True), "pdfimages"),
])
def test_which_tools_are_missing(capsys, comic, have, expected):
    for present, name in zip(have, ("pdfinfo", "pdfimages", "pdftoppm"), strict=True):
        if present:
            comic.install(name)
    assert comicpdf.comic_pdf_missing_tools() == 0
    assert capsys.readouterr().out == expected


def test_the_knobs_come_from_the_environment(capsys, comic, monkeypatch):
    comic.install("pdfinfo")
    comic.install("pdfimages")
    comic.say("pdfinfo", _info(1, size=(144, 144)))
    comic.say("pdfimages", _images(_img(1, 132, 133, "72", "72")))
    monkeypatch.setenv("comicPdfMinPixels", "100")
    # 84.7 percent of the page: the default 80 takes it, a raised 95 does not.
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 1 150\n"
    comic.clear()
    monkeypatch.setenv("comicPdfMinCoverage", "95")
    assert comicpdf.comic_pdf_stats("/book.pdf") == 0
    assert capsys.readouterr().out == "1 0 300\n"