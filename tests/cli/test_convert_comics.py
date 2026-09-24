"""The white box for medialib/cli/convert_comics.py.

What is pinned here is the archive FLATTENING: whatever container a book arrives
in, its pages end up in one folder, a cross-folder name collision keeps both
pages, and everything that is not a page is pruned. The extractors are stubbed on
PATH, because the container is the only thing that differs between the cases and
what is being tested is everything after it.
"""

import os
import stat
import zipfile

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
        # A real container, holding the names the stub above lays out: the
        # unpacking is stubbed, but what the archive ASKS for is read from the
        # archive itself before any unpacker runs.
        archive = tmp_path / "book.cbz"
        with zipfile.ZipFile(str(archive), "w") as packed:
            for name in ("00.jpg", "sub1/01.jpg", "sub1/02.jpg",
                         "sub2/01.jpg", "sub2/notes.txt"):
                packed.writestr(name, "x")
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
        # Two commands, because the real tool is asked two things: what is in
        # the archive, and then - only if the answer is a tree that stays inside
        # its own folder - unpack it.
        _stub(str(stub_bin), "7z", '''
printf "%%s\\n" "$*" >> "%s"
if [[ "$1" == "l" ]]; then
    printf -- "----------\\n"
    printf "Path = chapter one\\nAttributes = D drwxr-xr-x\\n\\n"
    printf "Path = chapter one/01.jpg\\nAttributes = A -rw-r--r--\\n\\n"
    printf "Path = chapter one/02.jpg\\nAttributes = A -rw-r--r--\\n\\n"
    printf "Path = ComicInfo.xml\\nAttributes = A -rw-r--r--\\n\\n"
    exit 0
fi
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


class TestPagesPerBook:
    """The run keeps the same number of pages converting whatever the number of
    books: fewer books than workers each take a wider share of it."""

    @pytest.mark.parametrize("books,expected", [
        (1, 16), (2, 8), (3, 5), (4, 4), (100, 4),
    ])
    def test_the_slots_are_shared_by_the_books_there_are(self, books,
                                                          expected):
        assert cc.pages_per_book(32, 4, books) == expected

    def test_never_fewer_than_a_full_pool_s_share(self):
        assert cc.pages_per_book(4, 4, 1) == cc.IMAGES_PER_BOOK

    def test_a_faster_speed_holds_fewer_cores_so_more_books_run(self):
        """At -s 9 a page keeps 2.5 cores busy, not 4."""
        assert cc.book_workers(32, 9) == 6
        assert cc.book_workers(32, 9) > cc.book_workers(32, 4)


# identify answers a page's first line as its size; compare calls two pages the
# same picture when their second lines match. Either fails outright on a page
# whose first line is "fail".
_IDENTIFY = """
page="${@: -1}"; page="${page%\\[0\\]}"
size="$(head -n1 "$page")"
[[ "$size" == fail ]] && exit 1
printf "%s" "$size"
"""
_COMPARE = """
printf "%s\n" "$*" >> "$(dirname "$0")/compare.log"
a="${3%\\[0\\]}"; b="${4%\\[0\\]}"
if [[ "$(sed -n 2p "$a")" == "$(sed -n 2p "$b")" ]]; then
    printf "655 (0.01)" >&2
else
    printf "28277 (0.43)" >&2
fi
exit 1
"""


class TestDropAlternateFormats:
    """A page the book holds twice, in two formats, is kept once - but only on
    evidence that the two files are the same picture."""

    @pytest.fixture
    def pages(self, tmp_path, stub_bin):
        _stub(str(stub_bin), "identify", _IDENTIFY)
        _stub(str(stub_bin), "compare", _COMPARE)
        book = tmp_path / "book"
        book.mkdir()

        def write(name, size="2560 3473", picture=None):
            (book / name).write_text("%s\n%s\n" % (size, picture or name))

        return book, write

    def test_the_psd_beside_its_export_is_dropped(self, pages):
        book, write = pages
        write("014.jpg")
        write("015.jpg", picture="p15")
        write("015.psd", picture="p15")
        write("016.jpg")
        assert cc.drop_alternate_formats(str(book)) == ["015.psd"]
        assert sorted(os.listdir(str(book))) == ["014.jpg", "015.jpg",
                                                 "016.jpg"]

    def test_the_copy_kept_is_in_the_book_s_own_format(self, pages):
        book, write = pages
        write("01.png")
        write("02.png", picture="p2")
        write("02.jpg", picture="p2")
        assert cc.drop_alternate_formats(str(book)) == ["02.jpg"]

    def test_two_different_pictures_under_one_name_are_both_kept(self, pages):
        book, write = pages
        write("015.jpg", picture="p15")
        write("015.psd", picture="a different page")
        assert cc.drop_alternate_formats(str(book)) == []
        assert len(os.listdir(str(book))) == 2

    def test_a_different_size_is_a_different_page_uncompared(self, pages,
                                                             stub_bin):
        book, write = pages
        write("015.jpg", picture="p15")
        write("015.psd", size="1280 1736", picture="p15")
        assert cc.drop_alternate_formats(str(book)) == []
        assert not (stub_bin / "compare.log").exists()

    def test_a_page_that_cannot_be_measured_is_kept(self, pages):
        book, write = pages
        write("015.jpg", picture="p15")
        write("015.psd", size="fail", picture="p15")
        assert cc.drop_alternate_formats(str(book)) == []

    def test_a_book_without_shared_names_is_not_compared(self, pages,
                                                         stub_bin):
        book, write = pages
        write("01.jpg")
        write("02.jpg")
        assert cc.drop_alternate_formats(str(book)) == []
        assert not (stub_bin / "compare.log").exists()


class TestTheStarvedShare:
    """Whether a book is converted at all, which is decided once for the WHOLE
    book: a .cbz whose pages are half AVIF and half JPEG is worse than either."""

    def test_a_book_past_the_share_is_left_as_it_is(self):
        # 9 of 10 measured pages starved, against a threshold of 80%
        assert cc.book_is_starved(9, 10)

    def test_one_exactly_at_it_is_not(self):
        """Strictly more, so a book that is exactly four fifths starved is
        converted - the boundary belongs to the side that does the work."""
        assert not cc.book_is_starved(8, 10)

    @pytest.mark.parametrize("starved,measured", [(0, 24), (5, 24), (19, 24)])
    def test_nor_is_one_below_it_however_many_thin_pages_it_holds(
            self, starved, measured):
        """The odd thin page is converted with the rest. Partial conversion is
        not on the table."""
        assert not cc.book_is_starved(starved, measured)

    def test_a_book_nothing_could_measure_is_converted(self):
        """The share is undefined, and the answer that converts it is the one
        every other unmeasurable thing in this repo gets."""
        assert not cc.book_is_starved(0, 0)

    def test_the_threshold_is_a_percentage_of_the_pages_that_were_measured(self):
        """Pages nothing could read are left out of both counts rather than
        counted as sound: an unreadable page is evidence about neither side."""
        assert cc.book_is_starved(9, 10)          # 90% of ten
        assert cc.book_is_starved(9, 10) == cc.book_is_starved(90, 100)


class TestCountingTheStarvedPages:
    def test_only_the_pages_are_counted(self, tmp_path, monkeypatch):
        """A folder of pages is what this is handed, but the pruning that made it
        one is a separate step and a caller may not have run it."""
        for name in ("a.jpg", "b.png", "notes.txt", "cover.webp"):
            (tmp_path / name).write_bytes(b"x")
        seen = []
        monkeypatch.setattr(cc.imagebitrate, "image_adequacy",
                            lambda path: seen.append(path) or "adequate")
        starved, measured = cc.starved_pages(str(tmp_path))
        assert (starved, measured) == (0, 3)
        assert not any(name.endswith(".txt") for name in seen)

    def test_a_page_that_could_not_be_judged_counts_for_neither_side(
            self, tmp_path, monkeypatch):
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            (tmp_path / name).write_bytes(b"x")
        answers = {"a.jpg": "starved", "b.jpg": "unknown", "c.jpg": "generous"}
        monkeypatch.setattr(
            cc.imagebitrate, "image_adequacy",
            lambda path: answers[os.path.basename(path)])
        assert cc.starved_pages(str(tmp_path)) == (1, 2)



class TestTheChromaReachesTheConversion:
    """-u is convert-images' to act on; this command settles the default -
    4:4:4, which convert-images does not have - and hands the value on to every
    book."""

    @staticmethod
    def _argv(tmp_path, monkeypatch, chroma):
        calls = []
        monkeypatch.setattr(
            cc.commands, "run_command",
            lambda name, argv, **kwargs: calls.append((name, argv))
            or cc.subprocess.CompletedProcess(argv, 0))
        state = cc.Run(counters=cc.Counters(str(tmp_path)), script_dir="",
                       quality="45", chroma=chroma, speed_preset="4",
                       pages_per_book=4,
                       max_res=2960, fuzz="10")
        state._convert_pages(str(tmp_path / "pages"), str(tmp_path / "avif"),
                             "book.cbz")
        [(name, argv)] = calls
        assert name == "convert-images"
        return argv

    @pytest.mark.parametrize("chroma", ["420", "444"])
    def test_the_chroma_is_passed_on(self, tmp_path, monkeypatch, chroma):
        argv = self._argv(tmp_path, monkeypatch, chroma)
        assert argv[argv.index("-u") + 1] == chroma

    def test_the_value_is_checked_before_any_book(self):
        """convert-images would refuse it too, but once per book and deep
        into the run."""
        with pytest.raises(cc.clioptions.UsageError):
            cc.clioptions.parse(cc.spec("convert-comics"),
                                ["-u", "422", "in", "out"])
