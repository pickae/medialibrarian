"""The white box for medialib/cli/ingest_books.py.

test_ingest_books_cli.py drives the whole pipeline with stubbed converters;
what is pinned here are the helpers underneath it - the membership check, the
removal that has to get past read-only content, the epub cleaning, and the emit
that must never clobber.
"""

import stat
import sys

import pytest

from medialib.cli import ingest_books as ib
from medialib.lib import enums

pytestmark = pytest.mark.fs


class TestExtInList:
    def test_it_finds_a_convertible_extension(self):
        assert ib.ext_in_list("mobi", enums.BOOK_CONVERT_EXTENSIONS)

    @pytest.mark.parametrize("extension", ["pdf", "epub"])
    def test_it_rejects_a_non_member(self, extension):
        """A PDF is copied rather than converted, and an epub is already one."""
        assert not ib.ext_in_list(extension, enums.BOOK_CONVERT_EXTENSIONS)


posix_modes = pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod on Windows carries the read-only bit and nothing else")


class TestSafeRmrf:
    """What the grant in front of the removal is for: an epub can extract
    content nothing may write to, and the tree still has to go."""

    def test_a_read_only_file_does_not_stop_the_removal(self, tmp_path):
        work = tmp_path / "work"
        (work / "OEBPS").mkdir(parents=True)
        page = work / "OEBPS" / "page.xhtml"
        page.write_text("x")
        page.chmod(0o400)
        ib.safe_rmrf(str(work))
        assert not work.exists()

    @posix_modes
    def test_a_read_only_directory_does_not_stop_it_either(self, tmp_path):
        # the one that needs the grant on POSIX: an entry cannot be removed
        # until its own directory has write and execute
        work = tmp_path / "work"
        (work / "OEBPS").mkdir(parents=True)
        (work / "OEBPS" / "page.xhtml").write_text("x")
        (work / "OEBPS").chmod(0o500)
        work.chmod(0o500)
        ib.safe_rmrf(str(work))
        assert not work.exists()

    def test_nothing_to_remove_is_a_no_op(self, tmp_path):
        ib.safe_rmrf(str(tmp_path / "gone"), "", None)
        assert tmp_path.exists()

    @posix_modes
    def test_the_grant_reads_plus_X_and_not_plus_x(self, tmp_path):
        """u+rwX grants execute on a directory and on whatever already had an
        execute bit - a plain file does not become an executable one."""
        plain = tmp_path / "page.xhtml"
        plain.write_text("x")
        plain.chmod(0o400)
        already = tmp_path / "run.sh"
        already.write_text("x")
        already.chmod(0o401)          # execute for other, none for the owner
        inner = tmp_path / "OEBPS"
        inner.mkdir()
        inner.chmod(0o500)

        ib._grant_tree_access(str(tmp_path))

        assert stat.S_IMODE(plain.stat().st_mode) == 0o600
        assert stat.S_IMODE(already.stat().st_mode) == 0o701
        assert stat.S_IMODE(inner.stat().st_mode) == 0o700

    @posix_modes
    def test_a_symlink_is_passed_over_rather_than_followed(self, tmp_path):
        """chmod's own rule, and the reason the grant cannot reach out of the
        tree it was handed."""
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        outside.chmod(0o400)
        work = tmp_path / "work"
        work.mkdir()
        (work / "link.txt").symlink_to(outside)

        ib._grant_tree_access(str(work))

        assert stat.S_IMODE(outside.stat().st_mode) == 0o400


class TestCleanBookFolder:
    """Fonts and junk images go; the real images and everything else stay."""

    @pytest.fixture
    def book(self, tmp_path):
        for name in ("OEBPS/fonts/serif.ttf",
                     "OEBPS/fonts/sans.OTF",          # uppercase - still a font
                     "OEBPS/images/page1.jpg",
                     "OEBPS/images/cover.png",
                     "OEBPS/images/teaser_next_book.jpg",   # junk by substring
                     "OEBPS/images/BackAdd-banner.png",     # junk, case-blind
                     "OEBPS/content.opf"):                  # kept
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        # No geometry, so nothing is handed to ImageMagick: what is asserted
        # here is which files survive, not what they were resized to.
        ib.clean_book_folder(str(tmp_path))
        return tmp_path

    @pytest.mark.parametrize("gone", [
        "OEBPS/fonts/serif.ttf",
        "OEBPS/fonts/sans.OTF",
        "OEBPS/images/teaser_next_book.jpg",
        "OEBPS/images/BackAdd-banner.png",
    ])
    def test_the_fonts_and_the_junk_images_are_removed(self, book, gone):
        assert not (book / gone).exists()

    @pytest.mark.parametrize("kept", [
        "OEBPS/images/page1.jpg",
        "OEBPS/images/cover.png",
        "OEBPS/content.opf",
    ])
    def test_the_real_content_is_kept(self, book, kept):
        assert (book / kept).exists()


class TestResizeImage:
    def test_a_small_image_is_copied_rather_than_converted(self, tmp_path):
        """Below the limit there is nothing worth downscaling, and the name has
        to survive either way so the epub's references stay valid."""
        image = tmp_path / "page.jpg"
        image.write_bytes(b"x" * 10)
        calls = []
        ib.resize_image(str(image), "1600x1200",
                        run=lambda argv, **kw: calls.append(argv))
        assert calls == []
        assert image.read_bytes() == b"x" * 10

    def test_a_large_image_is_handed_to_the_converter(self, tmp_path):
        image = tmp_path / "page.jpg"
        image.write_bytes(b"x" * (ib.FILE_SIZE_LIMIT + 1))
        calls = []

        class _Done:
            returncode = 0

        def run(argv, **kwargs):
            calls.append(argv)
            # the converter writes the temporary the caller then moves back
            with open(argv[-1], "wb") as temporary:
                temporary.write(b"small")
            return _Done()

        ib.resize_image(str(image), "1600x1200", run=run)
        assert calls and calls[0][0] == "convert"
        assert "1600x1200>" in calls[0]
        assert image.read_bytes() == b"small"      # kept under its old name

    def test_a_converter_that_fails_leaves_the_original(self, tmp_path):
        image = tmp_path / "page.jpg"
        image.write_bytes(b"x" * (ib.FILE_SIZE_LIMIT + 1))

        class _Failed:
            returncode = 1

        ib.resize_image(str(image), "1600x1200",
                        run=lambda argv, **kw: _Failed())
        assert image.exists()


class TestEmitOutput:
    def test_the_first_book_is_moved_into_place(self, tmp_path):
        source, dest = tmp_path / "src", tmp_path / "dst"
        source.mkdir()
        dest.mkdir()
        (source / "a.epub").write_text("one")
        ib.emit_output(str(source / "a.epub"), str(dest / "book.epub"))
        assert (dest / "book.epub").read_text() == "one"
        assert not (source / "a.epub").exists()    # consumed by the move

    def test_a_collision_keeps_both(self, tmp_path):
        source, dest = tmp_path / "src", tmp_path / "dst"
        source.mkdir()
        dest.mkdir()
        (source / "a.epub").write_text("one")
        ib.emit_output(str(source / "a.epub"), str(dest / "book.epub"))
        (source / "b.epub").write_text("two")
        ib.emit_output(str(source / "b.epub"), str(dest / "book.epub"))
        assert (dest / "book (2).epub").exists()
        assert (dest / "book.epub").read_text() == "one"   # not overwritten

    def test_a_missing_source_is_a_silent_no_op(self, tmp_path):
        """A failed conversion simply produces no output for that book."""
        dest = tmp_path / "dst"
        dest.mkdir()
        ib.emit_output(str(tmp_path / "gone.epub"), str(dest / "ghost.epub"))
        assert not (dest / "ghost.epub").exists()

    def test_the_destination_folder_is_made(self, tmp_path):
        source = tmp_path / "a.epub"
        source.write_text("one")
        ib.emit_output(str(source), str(tmp_path / "deep" / "down" / "b.epub"))
        assert (tmp_path / "deep" / "down" / "b.epub").exists()


class TestWordCount:
    @pytest.mark.parametrize("text,expected", [
        ("one two three\n", 3),
        ("", 0),
        ("   \n\n  \t ", 0),
        ("hyphen-word and\tanother\nline", 4),
    ])
    def test_it_counts_runs_of_non_whitespace_the_way_wc_does(self, tmp_path,
                                                              text, expected):
        path = tmp_path / "book.txt"
        path.write_text(text)
        assert ib._word_count(str(path)) == expected

    def test_a_file_that_is_not_there_counts_zero(self, tmp_path):
        assert ib._word_count(str(tmp_path / "gone.txt")) == 0
