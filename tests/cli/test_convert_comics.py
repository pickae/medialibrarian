"""The white box for medialib/cli/convert_comics.py.

What is pinned here is the archive FLATTENING: whatever container a book arrives
in, its pages end up in one folder, a cross-folder name collision keeps both
pages, and everything that is not a page is pruned. The extractors are stubbed on
PATH, because the container is the only thing that differs between the cases and
what is being tested is everything after it.
"""

import os
import stat

import pytest

from medialib.cli import convert_comics as cc

pytestmark = pytest.mark.fs


def _stub(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as handle:
        handle.write("#!/usr/bin/env bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def stub_bin(tmp_path, monkeypatch):
    """A PATH holding stub extractors, and nothing else that matters."""
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory) + os.pathsep
                       + os.environ.get("PATH", ""))
    return directory


class TestFlattening:
    """A nested archive laid out by the stub, then flattened by extract()."""

    @pytest.fixture
    def book(self, tmp_path, stub_bin):
        _stub(str(stub_bin), "unzip", '''
dest=""; prev=""
for a in "$@"; do [[ "$prev" == "-d" ]] && dest="$a"; prev="$a"; done
mkdir -p "$dest/sub1" "$dest/sub2"
printf a > "$dest/sub1/01.jpg"
printf b > "$dest/sub1/02.jpg"
printf c > "$dest/sub2/01.jpg"
printf x > "$dest/sub2/notes.txt"
printf d > "$dest/00.jpg"
exit 0
''')
        archive = tmp_path / "book.cbz"
        archive.write_text("")
        # The work folder is named after the whole FILE, extension included:
        # "book.cbz" and "book.pdf" side by side are two different books that
        # would otherwise be unpacked into one folder.
        destination = tmp_path / "work" / "book.cbz"
        cc.extract(str(archive), str(destination), 2960)
        return destination

    def test_the_tree_is_fully_flattened(self, book):
        below = [name for parent, _dirs, names in os.walk(str(book))
                 for name in names
                 if os.path.abspath(parent) != os.path.abspath(str(book))]
        assert below == []

    @pytest.mark.parametrize("page", ["00.jpg", "01.jpg", "02.jpg"])
    def test_a_page_that_did_not_collide_keeps_its_name(self, book, page):
        assert (book / page).exists()

    def test_the_colliding_page_is_kept_under_a_suffixed_name(self, book):
        """Both pages survive: the first keeps the name and the second takes the
        " (N)" suffix the rest of the repo gives a collision."""
        assert (book / "01 (2).jpg").exists()

    def test_exactly_four_pages_survive(self, book):
        """Nothing lost, nothing duplicated."""
        pages = [name for name in os.listdir(str(book))
                 if name.lower().endswith(".jpg")]
        assert len(pages) == 4

    def test_a_non_image_is_pruned(self, book):
        assert not (book / "notes.txt").exists()

    @pytest.mark.parametrize("folder", ["sub1", "sub2"])
    def test_the_emptied_subfolders_are_removed(self, book, folder):
        assert not (book / folder).exists()


class TestSevenZip:
    """The .cb7 path: a different tool fills the folder and everything after it
    is the code above. What is checked is that the archive really is dispatched
    to 7-Zip, with the arguments that KEEP its folders."""

    @pytest.fixture
    def book(self, tmp_path, stub_bin):
        log = tmp_path / "7z.args"
        _stub(str(stub_bin), "7z", '''
printf "%%s\\n" "$*" >> "%s"
dest=""
for a in "$@"; do [[ "$a" == -o* ]] && dest="${a#-o}"; done
mkdir -p "$dest/chapter one"
printf a > "$dest/chapter one/01.jpg"
printf b > "$dest/chapter one/02.jpg"
printf x > "$dest/ComicInfo.xml"
exit 0
''' % log)
        archive = tmp_path / "omnibus.cb7"
        archive.write_text("")
        destination = tmp_path / "work" / "omnibus.cb7"
        cc.extract(str(archive), str(destination), 2960)
        return destination, log

    def test_its_pages_are_flattened_into_the_book_folder(self, book):
        destination, _log = book
        pages = sorted(name for name in os.listdir(str(destination))
                       if name.lower().endswith(".jpg"))
        assert pages == ["01.jpg", "02.jpg"]

    def test_its_metadata_file_is_pruned(self, book):
        destination, _log = book
        assert not (destination / "ComicInfo.xml").exists()

    def test_and_its_emptied_subfolder_removed(self, book):
        destination, _log = book
        assert not (destination / "chapter one").exists()

    def test_seven_zip_extracts_WITH_the_archive_s_paths(self, book):
        """x, not e: the archive's own folders are kept, and the flattening above
        is what deals with them - together with the collisions it can produce."""
        destination, log = book
        assert "x -y -o%s" % destination in log.read_text()


class TestBookWorkers:
    """Books, not pages, is what scales with the host - that is the axis costing
    RAM, since one more book worker is one more book resident."""

    @pytest.mark.parametrize("threads,expected", [
        (32, 4), (64, 8), (16, 2),
    ])
    def test_the_pool_is_a_whole_number_of_books(self, threads, expected):
        assert cc.book_workers(threads) == expected

    @pytest.mark.parametrize("threads", [1, 2, 4, 8])
    def test_two_is_the_floor(self, threads):
        """With a single worker there is no second book to convert through the
        first one's unpacking and zipping, which is half the point."""
        assert cc.book_workers(threads) == 2
