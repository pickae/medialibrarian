"""The white box for medialib/lib/contentcensus.py.

What is pinned here: that the derived enums are derived from the CENTRAL lists
rather than restated, that every suffix those lists claim lands in exactly
one report, and the shape of the one accumulator whose behaviour is not what its
name suggests.
"""

import os
import sys

import pytest

from medialib.lib import contentcensus as cc
from medialib.lib import enums

pytestmark = pytest.mark.pure


class TestTheUnionIsAStringAndNotAList:
    """The shell accumulates a STRING, and membership is tested by splitting it on
    whitespace. So an empty word is never a member of it, every empty word takes
    the "not seen yet" branch again, and appending nothing to a non-empty string
    still appends the separator. A list-and-join tidies all of that away into
    something the shell never says."""

    def test_the_ordinary_case(self):
        assert cc.census_union(["jpg", "png", "avif"]) == "jpg png avif"

    def test_duplicates_fold_case_blind_keeping_the_first_seen(self):
        assert cc.census_union(["jpg", "JPG", "png", "Jpg"]) == "jpg png"

    def test_an_empty_word_after_something_appends_the_separator_alone(self):
        assert cc.census_union(["jpg", ""]) == "jpg "

    def test_an_empty_word_before_anything_appends_nothing_at_all(self):
        assert cc.census_union(["", "jpg"]) == "jpg"

    def test_an_empty_word_in_the_middle_doubles_the_separator(self):
        assert cc.census_union(["jpg", "", "png"]) == "jpg  png"

    def test_nothing_but_empty_words_is_nothing(self):
        assert cc.census_union(["", "", ""]) == ""

    def test_no_words_at_all(self):
        assert cc.census_union([]) == ""


class TestTheDerivedEnums:
    """Not one new enum: the lists are the central ones, and this file only says
    which content type each belongs to and derives the two unions no single list
    already covers."""

    def test_the_video_list_is_both_halves(self):
        """.ts and .wmv are only in convertAudio's list, .vob and .m3u8 only in
        ingestMovies' - a library holds both kinds and neither is a superset."""
        video = cc.census_video_extensions().split()
        for extension in enums.VIDEO_EXTENSIONS:
            assert extension in video
        for extension in enums.SOURCE_VIDEO_EXTENSIONS:
            assert extension in video
        assert "ts" in video and "vob" in video

    def test_and_it_is_folded_rather_than_concatenated(self):
        video = cc.census_video_extensions().split()
        assert len(video) == len(set(video))

    def test_the_page_list_is_every_scan_format_plus_what_we_emit(self):
        """avif is what the comic and image conversions EMIT, so a converted
        library censuses as one instead of as a stack of archives holding no
        pages."""
        pages = cc.census_page_extensions().split()
        for extension in enums.IMAGE_EXTENSIONS:
            assert extension in pages
        for extension in enums.COVER_IMAGE_EXTENSIONS:
            assert extension in pages
        assert "avif" in pages

    def test_the_all_list_covers_every_type_the_census_reports(self):
        every = cc.census_all_extensions().split()
        for names in (enums.AUDIO_EXTENSIONS, enums.COMIC_EXTENSIONS,
                      enums.COMIC_PDF_EXTENSIONS, enums.BOOK_INPUT_EXTENSIONS):
            for extension in names:
                assert extension in every
        for extension in cc.census_video_extensions().split():
            assert extension in every


class TestClassify:
    """Every suffix the central lists claim lands in exactly one report, and the
    PDF - which two of them claim - is decided by a probe instead."""

    @pytest.fixture(autouse=True)
    def _no_poppler(self, monkeypatch):
        monkeypatch.delenv("CENSUS_HAVE_POPPLER", raising=False)
        monkeypatch.delenv("censusVideoExtensions", raising=False)
        monkeypatch.delenv("comicPdfExtensions", raising=False)

    @pytest.mark.parametrize("extension", enums.AUDIO_EXTENSIONS)
    def test_every_audio_suffix_is_audio(self, extension):
        assert cc.census_classify("/x." + extension)[0] == "audio"

    @pytest.mark.parametrize("extension", enums.COMIC_EXTENSIONS)
    def test_every_comic_suffix_is_comics(self, extension):
        assert cc.census_classify("/x." + extension)[0] == "comics"

    def test_every_video_suffix_is_video(self):
        for extension in cc.census_video_extensions().split():
            assert cc.census_classify("/x." + extension)[0] == "video"

    def test_every_book_suffix_but_pdf_is_books(self):
        for extension in enums.BOOK_INPUT_EXTENSIONS:
            if extension in enums.COMIC_PDF_EXTENSIONS:
                continue
            assert cc.census_classify("/x." + extension)[0] == "books"

    def test_a_suffix_no_list_claims_is_nothing(self):
        assert cc.census_classify("/x.zzz") == ("", "")

    def test_a_file_with_no_suffix_is_nothing(self):
        assert cc.census_classify("/noext") == ("", "")
        assert cc.census_classify("/x.") == ("", "")

    def test_the_suffix_is_read_case_blind(self):
        assert cc.census_classify("/x.MP3")[0] == "audio"
        assert cc.census_classify("/x.MKV")[0] == "video"

    def test_no_suffix_lands_in_two_reports(self):
        """The only one two lists claim is pdf, and that is the one the probe
        decides."""
        seen = {}
        for names, content in (
                (enums.AUDIO_EXTENSIONS, "audio"),
                (cc.census_video_extensions().split(), "video"),
                (enums.COMIC_EXTENSIONS, "comics"),
                (enums.BOOK_INPUT_EXTENSIONS, "books")):
            for extension in names:
                if extension in enums.COMIC_PDF_EXTENSIONS:
                    continue
                assert seen.setdefault(extension, content) == content

    def test_without_poppler_every_pdf_is_a_book(self, monkeypatch):
        """The conservative half: a book row loses only its page count, where a
        comic row would invent a container and a page resolution."""
        monkeypatch.setenv("CENSUS_HAVE_POPPLER", "")
        assert cc.census_classify("/x.pdf") == ("books", "")

    def test_with_poppler_the_probe_decides_and_its_numbers_are_kept(
            self, monkeypatch):
        """isComicPdf prints its stats whichever way it votes, precisely so one
        probe answers both "is it?" and "how many pages?"."""
        monkeypatch.setenv("CENSUS_HAVE_POPPLER", "1")

        def voted_comic(pdf, max_height_px="0"):
            print("10 10 1")
            return 0

        def voted_book(pdf, max_height_px="0"):
            print("10 2 1")
            return 1

        monkeypatch.setattr(cc.comicpdf, "is_comic_pdf", voted_comic)
        assert cc.census_classify("/x.pdf") == ("comics", "10 10 1")
        monkeypatch.setattr(cc.comicpdf, "is_comic_pdf", voted_book)
        assert cc.census_classify("/x.pdf") == ("books", "10 2 1")

    def test_the_settled_video_list_is_honoured_when_the_caller_set_one(
            self, monkeypatch):
        monkeypatch.setenv("censusVideoExtensions", "zzz")
        assert cc.census_classify("/x.zzz")[0] == "video"
        assert cc.census_classify("/x.mkv")[0] == ""


class TestRow:
    def test_each_type_reaches_its_own_builder(self, monkeypatch):
        seen = []
        monkeypatch.setattr(cc.censusmedia, "census_audio_row",
                            lambda p, sep=None: (seen.append("audio"), "r")[1])
        monkeypatch.setattr(cc.censusmedia, "census_video_row",
                            lambda p, sep=None: (seen.append("video"), "r")[1])
        monkeypatch.setattr(cc.censusdocuments, "census_book_row",
                            lambda p, log=None: (seen.append("books"), "r")[1])
        monkeypatch.setattr(cc.censusdocuments, "census_comic_row",
                            lambda p, log=None: (seen.append("comics"), "r")[1])
        for content in ("audio", "video", "books", "comics"):
            cc.census_row(content, "/x")
        assert seen == ["audio", "video", "books", "comics"]

    def test_a_type_no_census_knows_is_refused_by_name(self):
        row, reason = cc.census_row("bogus", "/x")
        assert row is None
        assert reason == 'no census knows what a "bogus" is'

    def test_the_refusal_names_the_type_it_was_given(self):
        _row, reason = cc.census_row("", "/x")
        assert reason == 'no census knows what a "" is'


class TestInit:
    def test_it_settles_the_three_derived_lists(self, monkeypatch):
        for name in ("censusVideoExtensions", "censusPageExtensions",
                     "censusAllExtensions"):
            monkeypatch.delenv(name, raising=False)
        cc.census_init()
        assert os.environ["censusVideoExtensions"] == \
            cc.census_video_extensions()
        assert os.environ["censusPageExtensions"] == cc.census_page_extensions()
        assert os.environ["censusAllExtensions"] == cc.census_all_extensions()

    def test_it_settles_a_flag_for_every_optional_tool(self, monkeypatch):
        for name in ("CENSUS_HAVE_MEDIAINFO", "CENSUS_HAVE_POPPLER",
                     "CENSUS_HAVE_EBOOK_CONVERT", "CENSUS_HAVE_PDFTOTEXT",
                     "CENSUS_SEVENZIP"):
            monkeypatch.delenv(name, raising=False)
        cc.census_init()
        for name in ("CENSUS_HAVE_MEDIAINFO", "CENSUS_HAVE_POPPLER",
                     "CENSUS_HAVE_EBOOK_CONVERT", "CENSUS_HAVE_PDFTOTEXT",
                     "CENSUS_SEVENZIP"):
            assert name in os.environ

    def test_the_text_flag_is_separate_from_the_what_is_it_flag(self,
                                                               monkeypatch):
        """A host with poppler but without Calibre can still count its PDFs,
        which is why the two are asked separately rather than folded into one."""
        monkeypatch.setenv("PATH", "")
        cc.census_init()
        assert os.environ["CENSUS_HAVE_PDFTOTEXT"] == ""
        assert os.environ["CENSUS_HAVE_EBOOK_CONVERT"] == ""

    @pytest.mark.skipif(sys.platform == "win32",
                     reason="a shell-script stub with no Windows extension is "
                            "not discoverable there")
    def test_the_seven_zip_name_is_resolved_once(self, monkeypatch, tmp_path):
        """Its binary has three names in the wild - 7z from p7zip, 7zz upstream,
        7za in the reduced package."""
        (tmp_path / "7za").write_text("#!/bin/sh\n")
        os.chmod(str(tmp_path / "7za"), 0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        cc.census_init()
        assert os.environ["CENSUS_SEVENZIP"] == "7za"

    def test_and_is_empty_when_the_host_has_none_of_them(self, monkeypatch):
        monkeypatch.setenv("PATH", "")
        cc.census_init()
        assert os.environ["CENSUS_SEVENZIP"] == ""
