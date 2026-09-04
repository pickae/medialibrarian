"""The white box for medialib/lib/censusdocuments.py.

What is pinned here: the exact argv each tool is handed, the places where a
tool's STATUS is deliberately not read, and the field-level decisions - which
member is the first page, which suffix is a page at all, how a width that is not a
whole number is printed.
"""

import os
import shutil
from types import SimpleNamespace

import pytest

from medialib.lib import censusdocuments as cd
from tests import blackbox

pytestmark = pytest.mark.stubbed

_TOOLSTUB = blackbox.TOOLSTUB

_PLUMBING = ("bash", "awk", "cat", "base64", "wc", "stat")

_CENSUS_ENV = (
    "CENSUS_SEP", "CENSUS_HAVE_POPPLER", "CENSUS_HAVE_PDFTOTEXT",
    "CENSUS_HAVE_EBOOK_CONVERT", "CENSUS_PDF_STATS", "CENSUS_SCRATCH",
    "CENSUS_SEVENZIP", "censusPageExtensions", "comicPdfExtensions",
)


_table_line = blackbox.toolstub_table_line


@pytest.fixture()
def doc(tmp_path, monkeypatch):
    """A PATH holding only the stubs a test installs, plus the plumbing the
    module's own commands need."""
    bin_dir = tmp_path / "bin"
    out_dir = tmp_path / "stubout"
    state_dir = tmp_path / "stubstate"
    for directory in (bin_dir, out_dir, state_dir):
        directory.mkdir()
    for tool in _PLUMBING:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    record = tmp_path / "calls"

    def install(name):
        target = bin_dir / name
        shutil.copyfile(_TOOLSTUB, str(target))
        os.chmod(str(target), 0o755)
        return str(target)

    def say(name, text):
        (out_dir / name).write_text(text)

    def rc(name, codes):
        (out_dir / (name + ".rc")).write_text(codes + "\n")

    def table(name, lines):
        (out_dir / (name + ".table")).write_text("\n".join(lines) + "\n")

    def calls():
        if not record.exists():
            return []
        return [line.rstrip("\n").split("\t")[1:]
                for line in record.read_text().splitlines() if line]

    for name in _CENSUS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("TOOLSTUB_LOG", str(record))
    monkeypatch.setenv("TOOLSTUB_OUT", str(out_dir))
    monkeypatch.setenv("TOOLSTUB_STATE", str(state_dir))
    monkeypatch.setenv("LC_ALL", "C")
    return SimpleNamespace(install=install, say=say, rc=rc, table=table,
                           calls=calls, bin_dir=bin_dir, out_dir=out_dir,
                           tmp_path=tmp_path)


# --- the unzip pattern ----------------------------------------------------------


class TestZipPattern:
    def test_the_three_metacharacters_are_each_wrapped(self):
        assert cd.census_zip_pattern("a[b]c*d?e") == "a[[]b]c[*]d[?]e"

    def test_the_bracket_goes_first(self):
        """The replacements for * and ? introduce brackets of their own, so a
        bracket escaped after them would be escaped twice."""
        assert cd.census_zip_pattern("*") == "[*]"
        assert cd.census_zip_pattern("[") == "[[]"
        assert cd.census_zip_pattern("[*]") == "[[][*]]"

    def test_a_plain_name_is_itself(self):
        assert cd.census_zip_pattern("01 scan.jpg") == "01 scan.jpg"


class TestExtensionList:
    def test_the_suffixes_are_spelled_with_dots_and_slashes(self):
        assert cd.extension_list(["jpg", "jpeg", "png"]) == \
            ".jpg / .jpeg / .png"

    def test_one_suffix_has_no_separator(self):
        assert cd.extension_list(["jpg"]) == ".jpg"

    def test_no_suffix_is_nothing(self):
        assert cd.extension_list([]) == ""


# --- the books row --------------------------------------------------------------


class TestBookRow:
    def _book(self, doc, name, text="a b c"):
        path = doc.tmp_path / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_a_txt_is_counted_where_it_lies(self, doc):
        book = self._book(doc, "b.txt", "one two three")
        row, reason = cd.census_book_row(book)
        assert reason is None
        assert row.split(",")[2:] == ["", "3", "13"]
        # no conversion, so no tool was reached for at all
        assert doc.calls() == []

    def test_a_pdf_page_count_comes_from_the_probe_already_run(self, doc,
                                                              monkeypatch):
        book = self._book(doc, "b.pdf")
        monkeypatch.setenv("CENSUS_HAVE_POPPLER", "1")
        monkeypatch.setenv("CENSUS_PDF_STATS", "42 3 1")
        row, reason = cd.census_book_row(book)
        assert reason is None
        assert row.split(",")[2] == "42"
        # pdfinfo is NOT asked again
        assert [call[0] for call in doc.calls()] == []

    @pytest.mark.parametrize("stats", ["0 3 1", "abc 3 1", "", "-1 3 1"])
    def test_a_pdf_whose_page_count_is_not_one_is_not_a_pdf(self, doc,
                                                            monkeypatch, stats):
        book = self._book(doc, "b.pdf")
        monkeypatch.setenv("CENSUS_HAVE_POPPLER", "1")
        monkeypatch.setenv("CENSUS_PDF_STATS", stats)
        row, reason = cd.census_book_row(book)
        assert row is None
        assert reason == "pdfinfo could not read it as a PDF"

    def test_without_poppler_the_question_was_never_asked(self, doc):
        """The column stays empty instead, and the row is still written - the run
        says once at startup that poppler is missing."""
        book = self._book(doc, "b.pdf")
        row, reason = cd.census_book_row(book)
        assert reason is None
        assert row.split(",")[2] == ""

    def test_a_pdf_is_convertible_by_either_tool(self, doc, monkeypatch,
                                                 tmp_path):
        book = self._book(doc, "b.pdf")
        doc.install("pdftotext")
        doc.say("pdftotext", "one two")
        doc.rc("pdftotext", "0")
        doc.table("pdftotext", [])
        monkeypatch.setenv("CENSUS_HAVE_PDFTOTEXT", "1")
        monkeypatch.setenv("CENSUS_SCRATCH", str(tmp_path))
        (doc.out_dir / "pdftotext.write").write_text("$LAST\n")
        row, reason = cd.census_book_row(book)
        assert reason is None
        assert doc.calls()[0][0] == "pdftotext"
        # "one two" - two words, seven characters, no newline of its own
        assert row.split(",")[3:] == ["2", "7"]

    def test_and_everything_else_only_by_ebook_convert(self, doc, monkeypatch):
        book = self._book(doc, "b.epub")
        doc.install("pdftotext")
        monkeypatch.setenv("CENSUS_HAVE_PDFTOTEXT", "1")
        row, reason = cd.census_book_row(book)
        assert reason is None
        # the columns stay empty and the row is still written
        assert row.split(",")[3:] == ["", ""]
        assert doc.calls() == []

    def test_a_conversion_that_fails_skips_the_book(self, doc, monkeypatch,
                                                    tmp_path):
        book = self._book(doc, "b.epub")
        doc.install("ebook-convert")
        doc.rc("ebook-convert", "1")
        monkeypatch.setenv("CENSUS_HAVE_EBOOK_CONVERT", "1")
        monkeypatch.setenv("CENSUS_SCRATCH", str(tmp_path))
        row, reason = cd.census_book_row(book)
        assert row is None
        assert reason == ("its text could not be read (it may be encrypted or "
                          "truncated)")

    def test_the_scratch_text_never_survives_the_row(self, doc, monkeypatch,
                                                     tmp_path):
        book = self._book(doc, "b.epub")
        doc.install("ebook-convert")
        doc.say("ebook-convert", "one two")
        doc.rc("ebook-convert", "0")
        (doc.out_dir / "ebook-convert.write").write_text("$LAST\n")
        monkeypatch.setenv("CENSUS_HAVE_EBOOK_CONVERT", "1")
        monkeypatch.setenv("CENSUS_SCRATCH", str(tmp_path))
        row, reason = cd.census_book_row(book)
        assert reason is None
        assert not os.path.exists(tmp_path / "censusBook.txt")

    def test_the_separator_the_caller_chose_is_the_row_s(self, doc,
                                                        monkeypatch):
        book = self._book(doc, "b.txt", "one")
        monkeypatch.setenv("CENSUS_SEP", "\t")
        row, _reason = cd.census_book_row(book)
        assert "\t" in row and "," not in row.split("\t")[0]


# --- the comics row -------------------------------------------------------------


class TestComicArchiveRow:
    def _comic(self, doc, name):
        path = doc.tmp_path / name
        path.write_bytes(b"xx")
        return str(path)

    def _zip(self, doc, comic, members, rc=0):
        doc.install("unzip")
        doc.table("unzip", [
            _table_line(["-Z1", "--", comic], rc, "\n".join(members) + "\n")])

    def test_the_first_page_is_the_first_in_natural_order(self, doc):
        comic = self._comic(doc, "c.cbz")
        self._zip(doc, comic, ["10.jpg", "2.jpg", "1.jpg"])
        doc.install("identify")
        doc.say("identify", "JPEG 8x9\n")
        doc.say("unzip", "PAGE")
        row, reason = cd.census_comic_archive_row(comic, "cbz")
        assert reason is None
        extraction = [call for call in doc.calls() if call[1] == "-p"]
        assert extraction[0][-1] == "1.jpg"
        assert row.split(",")[2] == "3"

    def test_only_the_images_are_pages(self, doc):
        comic = self._comic(doc, "c.cbz")
        self._zip(doc, comic,
                  ["ComicInfo.xml", "Thumbs.db", "01.jpg", "notes", ".hidden"])
        doc.install("identify")
        doc.say("identify", "JPEG 8x9\n")
        doc.say("unzip", "PAGE")
        row, _reason = cd.census_comic_archive_row(comic, "cbz")
        assert row.split(",")[2] == "1"

    def test_a_name_whose_only_dot_is_its_first_has_no_suffix(self, doc):
        comic = self._comic(doc, "c.cbz")
        self._zip(doc, comic, [".jpg"])
        row, reason = cd.census_comic_archive_row(comic, "cbz")
        assert row is None
        assert reason.startswith("it holds no image page")

    def test_an_archive_that_will_not_open_is_skipped(self, doc):
        comic = self._comic(doc, "c.cbz")
        self._zip(doc, comic, ["01.jpg"], rc=1)
        row, reason = cd.census_comic_archive_row(comic, "cbz")
        assert row is None
        assert reason == "it could not be opened as a zip archive"

    def test_an_archive_holding_no_page_names_what_it_looked_for(self, doc):
        comic = self._comic(doc, "c.cbz")
        self._zip(doc, comic, ["read.txt"])
        row, reason = cd.census_comic_archive_row(comic, "cbz")
        assert row is None
        assert reason == ("it holds no image page (looked for .jpg / .jpeg / "
                          ".webp / .png / .svg / .tiff / .tif / .bmp / .avif)")

    def test_an_unreadable_first_page_is_warned_about_not_skipped(self, doc):
        comic = self._comic(doc, "c.cbz")
        self._zip(doc, comic, ["01.jpg"])
        doc.install("identify")
        doc.say("identify", "\n")
        doc.say("unzip", "PAGE")
        said = []
        row, reason = cd.census_comic_archive_row(comic, "cbz", said.append)
        assert reason is None
        fields = row.split(",")
        assert fields[2] == "1" and fields[3] == "" and fields[4] == "zip"
        assert len(said) == 2
        assert "could not be read" in said[0]

    def test_the_container_is_the_extractor_that_opened_it(self, doc):
        comic = self._comic(doc, "c.cbr")
        doc.install("unrar")
        doc.table("unrar", [_table_line(["lb", comic], 0, "01.jpg\n")])
        doc.say("unrar", "PAGE")
        doc.install("identify")
        doc.say("identify", "PNG 1x2\n")
        row, _reason = cd.census_comic_archive_row(comic, "cbr")
        assert row.split(",")[4] == "rar"
        assert [call[0:2] for call in doc.calls() if call[0] == "unrar"][0] \
            == ["unrar", "lb"]

    def test_a_seven_zip_listing_is_filtered_to_its_paths(self, doc,
                                                          monkeypatch):
        comic = self._comic(doc, "c.cb7")
        sevenzip = doc.install("7zz")
        monkeypatch.setenv("CENSUS_SEVENZIP", sevenzip)
        doc.table("7zz", [
            _table_line(["l", "-ba", "-slt", comic], 0,
                        "Path = a/01.jpg\nSize = 4\nPath = b/02.jpg\n")])
        doc.say("7zz", "PAGE")
        doc.install("identify")
        doc.say("identify", "JPEG 5x6\n")
        row, reason = cd.census_comic_archive_row(comic, "cb7")
        assert reason is None
        assert row.split(",")[2] == "2"
        assert row.split(",")[4] == "7z"

    def test_without_a_seven_zip_the_archive_will_not_open(self, doc):
        comic = self._comic(doc, "c.cb7")
        row, reason = cd.census_comic_archive_row(comic, "cb7")
        assert row is None
        assert reason == "it could not be opened as a 7z archive"

    def test_a_suffix_no_extractor_claims_names_no_container(self, doc):
        comic = self._comic(doc, "c.zip")
        row, reason = cd.census_comic_archive_row(comic, "zip")
        assert row is None
        assert reason == "it could not be opened as a  archive"


class TestComicPdfRow:
    def _pdf(self, doc, listing, rc=0):
        pdf = doc.tmp_path / "c.pdf"
        pdf.write_bytes(b"xx")
        doc.install("pdfimages")
        doc.say("pdfimages", listing)
        doc.rc("pdfimages", str(rc))
        return str(pdf)

    HEADER = ("page   num  type   width height color comp bpc  enc interp  "
              "object ID x-ppi y-ppi size ratio\n")

    def test_the_first_image_row_of_page_one_is_the_page(self, doc,
                                                        monkeypatch):
        pdf = self._pdf(doc, self.HEADER
                        + "   1     0 image    1600  2400  gray    1   8  "
                          "jpeg  no        12  0   150   150  100K  1.0%\n")
        monkeypatch.setenv("CENSUS_PDF_STATS", "42 3 1")
        row, reason = cd.census_comic_pdf_row(pdf)
        assert reason is None
        assert row.split(",")[2:] == ["42", "1600x2400", "pdf", "jpeg"]

    def test_a_smask_row_is_an_alpha_channel_not_an_image(self, doc):
        pdf = self._pdf(doc, self.HEADER
                        + "   1     0 smask       8     9  gray    1   8  "
                          "flate no        12  0   150   150  1K  1.0%\n"
                        + "   1     1 image    1600  2400  gray    1   8  "
                          "jpeg  no        13  0   150   150  100K  1.0%\n")
        row, _reason = cd.census_comic_pdf_row(pdf)
        assert row.split(",")[3] == "1600x2400"

    def test_a_stencil_row_is_a_page(self, doc):
        pdf = self._pdf(doc, self.HEADER
                        + "   1     0 stencil   100   200  gray    1   1  "
                          "ccitt no        12  0   150   150  1K  1.0%\n")
        row, _reason = cd.census_comic_pdf_row(pdf)
        assert row.split(",")[3:] == ["100x200", "pdf", "ccitt"]

    def test_the_codec_is_lowercased(self, doc):
        pdf = self._pdf(doc, self.HEADER
                        + "   1     0 image     10    20  gray    1   8  "
                          "JPX   no        12  0   150   150  1K  1.0%\n")
        row, _reason = cd.census_comic_pdf_row(pdf)
        assert row.split(",")[5] == "jpx"

    def test_a_width_that_is_not_a_whole_number_is_truncated(self, doc):
        """awk's %d truncates toward zero. The shell's printf '%.0f' would round
        12.7 up to 13, and this is the awk one."""
        pdf = self._pdf(doc, self.HEADER
                        + "   1     0 image   12.7   0.9  gray    1   8  "
                          "jpeg  no        12  0   150   150  1K  1.0%\n")
        row, _reason = cd.census_comic_pdf_row(pdf)
        assert row.split(",")[3] == "12x0"

    def test_a_listing_the_probe_printed_and_then_failed_is_still_read(self,
                                                                      doc):
        """The shell PIPES pdfimages into awk, so the status is the pipeline's
        last stage and what the tool printed is the answer."""
        pdf = self._pdf(doc, self.HEADER
                        + "   1     0 image    800  1200  gray    1   8  "
                          "jpeg  no        12  0   150   150  1K  1.0%\n",
                        rc=7)
        row, _reason = cd.census_comic_pdf_row(pdf)
        assert row.split(",")[3] == "800x1200"

    def test_a_row_too_short_to_be_one_is_walked_over(self, doc):
        pdf = self._pdf(doc, self.HEADER + "   1     0 image    800\n")
        row, _reason = cd.census_comic_pdf_row(pdf)
        assert row.split(",")[3] == ""

    def test_without_pdfimages_both_columns_stay_empty(self, doc,
                                                       monkeypatch):
        pdf = doc.tmp_path / "c.pdf"
        pdf.write_bytes(b"xx")
        monkeypatch.setenv("CENSUS_PDF_STATS", "7 3 1")
        row, reason = cd.census_comic_pdf_row(str(pdf))
        assert reason is None
        assert row.split(",")[2:] == ["7", "", "pdf", ""]


class TestComicRowDispatch:
    def test_a_pdf_goes_to_the_pdf_row(self, doc, monkeypatch):
        pdf = doc.tmp_path / "c.pdf"
        pdf.write_bytes(b"xx")
        monkeypatch.setenv("CENSUS_PDF_STATS", "3 1 1")
        row, _reason = cd.census_comic_row(str(pdf))
        assert row.split(",")[4] == "pdf"

    def test_and_everything_else_to_the_archive_row(self, doc):
        comic = doc.tmp_path / "c.cbz"
        comic.write_bytes(b"xx")
        row, reason = cd.census_comic_row(str(comic))
        assert row is None
        assert reason == "it could not be opened as a zip archive"


class TestFirstPage:
    def test_the_zip_arm_quotes_the_member_into_a_pattern(self, doc):
        comic = doc.tmp_path / "c.cbz"
        comic.write_bytes(b"xx")
        doc.install("unzip")
        doc.say("unzip", "PAGE")
        assert cd.census_first_page(str(comic), "cbz", "01 [s].jpg") == b"PAGE"
        assert doc.calls()[0][-1] == "01 [[]s].jpg"

    def test_the_rar_arm_hands_the_name_over_as_it_stands(self, doc):
        comic = doc.tmp_path / "c.cbr"
        comic.write_bytes(b"xx")
        doc.install("unrar")
        doc.say("unrar", "PAGE")
        assert cd.census_first_page(str(comic), "cbr", "01 [s].jpg") == b"PAGE"
        assert doc.calls()[0] == ["unrar", "p", "-inul", str(comic),
                                  "01 [s].jpg"]

    def test_an_extraction_that_printed_and_then_failed_is_still_the_page(
            self, doc):
        """The shell ends every arm with "|| true": the status is discarded on
        purpose, so a tool that wrote the page and then complained has still
        written it."""
        comic = doc.tmp_path / "c.cbz"
        comic.write_bytes(b"xx")
        doc.install("unzip")
        doc.say("unzip", "PAGE")
        doc.rc("unzip", "7")
        assert cd.census_first_page(str(comic), "cbz", "01.jpg") == b"PAGE"

    def test_a_suffix_no_extractor_claims_is_no_page(self, doc):
        assert cd.census_first_page("/x.zip", "zip", "01.jpg") == b""

    def test_an_absent_tool_is_no_page(self, doc):
        assert cd.census_first_page("/x.cbz", "cbz", "01.jpg") == b""
