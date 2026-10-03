"""`ingest-books` end to end: a tree of sources in, one book each out.

Every heavy tool is a stub, so no real books and no converter are involved. What
that leaves is the control flow no helper's own cases reach: the mirrored tree,
which sources take the unpack-clean-repack path and which do not, the collision
suffix, the -d gate in front of everything that discards part of a book, and the
progress counter.

The counter is the interesting one. It counts books FINISHED against the number of
input books, so every path through a book has to count it exactly once - and the
path that converts has two things to say about one book, which is how a counter
reaches "[8/5]" over five of them. The intermediate steps are uncounted, indented
notes.
"""

from __future__ import annotations

import re

import pytest

from tests import blackbox

pytestmark = pytest.mark.stubbed

_EBOOK_CONVERT = r'printf epub > "$2"'

_GHOSTSCRIPT = r"""
for a in "$@"; do
    case "$a" in -sOutputFile=*) printf pdf > "${a#-sOutputFile=}";; esac
done
"""

# A fixed unpacked epub tree with a font and a junk image, so the folder cleaner
# has something to prune.
_UNZIP = r"""
dest=""; prev=""
for a in "$@"; do [[ "$prev" == "-d" ]] && dest="$a"; prev="$a"; done
mkdir -p "$dest/OEBPS/images" "$dest/OEBPS/fonts"
printf x > "$dest/OEBPS/content.opf"
printf x > "$dest/OEBPS/fonts/embedded.ttf"
printf x > "$dest/OEBPS/images/page.jpg"
printf x > "$dest/OEBPS/images/teaser_ad.jpg"
"""

_ZIP = r"""
arch=""
for a in "$@"; do case "$a" in -*|.) ;; *) arch="$a"; break;; esac; done
[[ -n "$arch" ]] && printf zip > "$arch"
"""

_CONVERT = r'out="${!#}"; out="${out%\>}"; : > "$out"'


def _counted(log: str) -> list[tuple[int, int]]:
    """The "[n/total]" lines, as (n, total) pairs."""
    return [(int(n), int(total)) for n, total
            in re.findall(r"^\[(\d+)/(\d+)\]", log, re.MULTILINE)]


@pytest.fixture
def books(sandbox, tmp_path):
    """The stubs, and a nested input tree covering every source kind.

    Every tool that only ever throws part of a book away logs its own name as it
    runs, which is how a case can say that a default run never reached for one.
    """
    calls = tmp_path / "calls"

    def discarding(name, body):
        sandbox.with_tool(name,
                          'echo %s >> %s\n%s' % (name, calls, body))

    sandbox.with_tool("ebook-convert", _EBOOK_CONVERT)
    discarding("gs", _GHOSTSCRIPT)
    discarding("unzip", _UNZIP)
    discarding("zip", _ZIP)
    discarding("convert", _CONVERT)

    source = tmp_path / "in"
    for relative in ("fiction/novel.mobi",
                     "fiction/story.epub",
                     # The same stem as story.epub, so the output collides.
                     "fiction/story.txt",
                     "manuals/guide.pdf",
                     "notes.txt",
                     # Not a book at all.
                     "cover.jpg"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    outputs = tmp_path / "out"

    def run(*flags):
        done = sandbox.run("ingest-books", *flags, source, outputs,
                           timeout=600)
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout + done.stderr

    def discarded():
        """Which discarding tools this run reached for, in name order."""
        if not calls.exists():
            return []
        return sorted(set(calls.read_text().split()))

    sandbox.source = source
    sandbox.outputs = outputs
    sandbox.ingest = run
    sandbox.discarded = discarded
    return sandbox


class TestTheTreeARunProduces:
    @pytest.fixture
    def run(self, books):
        before = blackbox.tree_of(books.source)
        return books, books.ingest(), before

    def test_every_source_kind_becomes_a_book_in_the_mirrored_folder(self, run):
        """mobi, txt and epub all end up epub; a PDF is copied as a PDF and skips
        the unpack-clean-repack path entirely."""
        books, _, _ = run
        assert (books.outputs / "fiction" / "novel.epub").is_file()
        assert (books.outputs / "fiction" / "story.epub").is_file()
        assert (books.outputs / "notes.epub").is_file()
        assert (books.outputs / "manuals" / "guide.pdf").is_file()

    def test_two_sources_of_one_stem_both_survive(self, run):
        books, _, _ = run
        assert (books.outputs / "fiction" / "story (2).epub").is_file()

    def test_a_source_that_is_not_a_book_produces_nothing(self, run):
        books, _, _ = run
        assert not (books.outputs / "cover.epub").exists()

    def test_nothing_is_lost_and_nothing_duplicated(self, run):
        books, _, _ = run
        assert len(list(books.outputs.rglob("*.epub"))) == 4
        assert len(list(books.outputs.rglob("*.pdf"))) == 1

    def test_the_input_tree_is_left_completely_untouched(self, run):
        books, _, before = run
        assert blackbox.tree_of(books.source) == before


class TestTheDiscardGate:
    """Everything a reader cannot get back from the output - the embedded fonts,
    the junk images, the illustrations' resolution, a PDF's images - is behind
    -d. So a default run must not reach for a single tool whose only job is to
    take part of a book away, and must still emit every book."""

    def test_a_default_run_reaches_for_no_discarding_tool_at_all(self, books):
        books.ingest()
        assert books.discarded() == []

    def test_a_default_run_says_which_version_it_is_making(self, books):
        assert "kept whole" in books.ingest()

    def test_a_default_run_still_emits_every_book(self, books):
        books.ingest()
        assert len(list(books.outputs.rglob("*.epub"))) == 4
        assert len(list(books.outputs.rglob("*.pdf"))) == 1

    def test_d_strips_the_pdf_and_opens_the_epubs_for_the_cleaner(self, books):
        books.ingest("-d")
        assert {"gs", "unzip", "zip"} <= set(books.discarded())

    def test_d_still_emits_every_book(self, books):
        books.ingest("-d")
        assert len(list(books.outputs.rglob("*.epub"))) == 4
        assert len(list(books.outputs.rglob("*.pdf"))) == 1

    def test_the_long_form_opens_the_same_gate(self, books):
        books.ingest("--discard-extras")
        assert {"gs", "unzip", "zip"} <= set(books.discarded())


class TestASecondRun:
    """Outputs older than the new run's marker are skipped, so no book is
    re-emitted - and the collision suffix from the first run must survive as it
    is rather than breeding another."""

    @pytest.fixture
    def run(self, books):
        books.ingest()
        return books, books.ingest()

    def test_no_extra_book_is_emitted(self, run):
        books, _ = run
        assert len(list(books.outputs.rglob("*.epub"))) == 4

    def test_the_collision_suffix_does_not_breed_another(self, run):
        books, _ = run
        assert not (books.outputs / "fiction" / "story (3).epub").exists()

    def test_a_skip_counts_its_book_exactly_once_too(self, run):
        _, log = run
        skips = re.findall(r"^\[\d+/5\] Skip \(exists\): ", log, re.MULTILINE)
        assert len(skips) == 5, log


class TestTheProgressCounter:
    """Five books go in, so there are exactly five counted lines, numbered 1 to 5
    with 5 as the denominator throughout. Three of them go through the converter,
    which is the path with two things to say about one book - so it is the path
    that can count it twice and take this run to "[8/5]"."""

    @pytest.fixture
    def log(self, books):
        return books.ingest()

    def test_there_is_one_counted_line_per_input_book(self, log):
        assert len(_counted(log)) == 5, log

    def test_the_counter_goes_one_to_five_each_exactly_once(self, log):
        assert sorted(n for n, _ in _counted(log)) == [1, 2, 3, 4, 5]

    def test_the_denominator_is_the_book_count_on_every_line(self, log):
        assert {total for _, total in _counted(log)} == {5}

    def test_the_slow_step_is_still_announced_but_uncounted(self, log):
        """The information the second counted line carried is not lost, just
        uncounted: an indented note, once per book that needs converting."""
        announced = re.findall(r"^\s+Converting: ", log, re.MULTILINE)
        assert len(announced) == 3, log


@pytest.fixture
def shelf(sandbox, tmp_path):
    """An empty input to stock with books of known contents, and the default
    stubs - with a converter that copies its source through, so a book's words
    reach its text. Text mode reads a PDF with pdftotext wherever there is one,
    so that is stubbed the same way rather than left to the host."""
    sandbox.with_tool("ebook-convert", r'cat "$1" > "$2"')
    sandbox.with_tool("pdftotext", r'cat "${@: -2:1}" > "${@: -1}"')
    sandbox.with_tool("gs", _GHOSTSCRIPT)
    sandbox.with_tool("unzip", _UNZIP)
    sandbox.with_tool("zip", _ZIP)
    sandbox.with_tool("convert", _CONVERT)
    source, outputs = tmp_path / "in", tmp_path / "out"
    source.mkdir()

    def stock(contents: dict[str, str]):
        for relative, text in contents.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def run(*flags):
        done = sandbox.run("ingest-books", *flags, source, outputs,
                           timeout=600)
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout + done.stderr

    sandbox.source = source
    sandbox.outputs = outputs
    sandbox.stock = stock
    sandbox.ingest = run
    return sandbox


def _counts_each_book_once(log: str, total: int) -> None:
    assert sorted(n for n, _ in _counted(log)) == list(range(1, total + 1)), log
    assert {t for _, t in _counted(log)} == {total}, log


class TestABookThatFailsIsStillCountedOnce:
    """A failure is reported for that book and the run goes on: the counter
    still reaches the number of books, each exactly once, and the failed book
    leaves nothing in the output."""

    def test_a_converter_that_fails_one_book(self, shelf):
        shelf.stock({"good.mobi": "fine", "broken.mobi": "bad",
                     "other.epub": "ok"})
        shelf.with_tool("ebook-convert",
                        r'[[ "$1" == *broken* ]] && exit 1; cat "$1" > "$2"')
        log = shelf.ingest()
        _counts_each_book_once(log, 3)
        assert re.search(r"^\[\d/3\] FAILED \(convert\): broken\.mobi$", log,
                         re.MULTILINE), log
        assert sorted(p.name for p in shelf.outputs.iterdir()) == [
            "good.epub", "other.epub"]

    def test_ghostscript_failing_under_d(self, shelf):
        shelf.stock({"scan.pdf": "pages", "novel.epub": "words"})
        shelf.with_tool("gs", "exit 1")
        log = shelf.ingest("-d")
        _counts_each_book_once(log, 2)
        assert re.search(r"^\[\d/2\] FAILED \(pdf\): scan\.pdf$", log,
                         re.MULTILINE), log
        assert sorted(p.name for p in shelf.outputs.iterdir()) == [
            "novel.epub"]

    def test_only_the_final_re_conversion_failing_still_emits_the_epub(
            self, shelf):
        """The re-conversion is the polish, not the book: when it fails, the
        epub that went in comes out instead, and that is a success."""
        shelf.stock({"novel.epub": "the epub as it came in"})
        shelf.with_tool("ebook-convert", "exit 1")
        log = shelf.ingest()
        _counts_each_book_once(log, 1)
        assert re.search(r"^\[1/1\] Done: novel\.epub$", log, re.MULTILINE), log
        assert (shelf.outputs / "novel.epub").read_text() \
            == "the epub as it came in"


class TestTextMode:
    """-t turns every book into one raw .txt in the mirrored folder, then word-
    counts them into a summary named after the output folder."""

    @pytest.fixture
    def run(self, shelf):
        shelf.stock({"fiction/novel.mobi": "one two three four",
                     "fiction/story.epub": "one",
                     "manuals/guide.pdf": "one two three four five six",
                     "notes.txt": "one two"})
        return shelf, shelf.ingest("-t")

    @staticmethod
    def _summary(shelf) -> list[tuple[int, str]]:
        rows = (shelf.outputs / "out.txt").read_text().splitlines()
        return [(int(words), path) for words, path
                in (row.split("\t") for row in rows)]

    def test_every_book_becomes_one_txt_in_its_mirrored_folder(self, run):
        shelf, _ = run
        assert blackbox.tree_of(shelf.outputs) == [
            "fiction", "fiction/novel.txt", "fiction/story.txt",
            "manuals", "manuals/guide.txt", "notes.txt", "out.txt"]
        assert (shelf.outputs / "fiction" / "novel.txt").read_text() \
            == "one two three four"

    def test_the_summary_is_sorted_by_word_count_most_first(self, run):
        shelf, _ = run
        assert [(words, path.rpartition("/")[2])
                for words, path in self._summary(shelf)] == [
            (6, "guide.txt"), (4, "novel.txt"), (2, "notes.txt"),
            (1, "story.txt")]

    def test_the_summary_does_not_count_itself(self, run):
        shelf, log = run
        assert not any(path.endswith("/out.txt")
                       for _, path in self._summary(shelf))
        assert "Total words across 4 file(s): 13" in log

    def test_each_book_is_counted_once(self, run):
        _, log = run
        _counts_each_book_once(log, 4)


class TestTextModeWhoseOutputFolderSharesAStem:
    """A top-level book whose stem is the output folder's name emits right
    onto the summary's own path. The book keeps the name and the summary
    moves to make room, so neither is lost and both are counted."""

    def test_the_book_keeps_the_name_and_the_summary_makes_room(self, shelf):
        shelf.stock({"out.mobi": "one two three",
                     "novel.epub": "one"})
        log = shelf.ingest("-t")
        assert (shelf.outputs / "out.txt").read_text() == "one two three"
        rows = (shelf.outputs / "out (2).txt").read_text().splitlines()
        assert [(int(words), path.rpartition("/")[2])
                for words, path in (row.split("\t") for row in rows)] == [
            (3, "out.txt"), (1, "novel.txt")]
        assert "Total words across 2 file(s): 4" in log

    def test_a_second_run_does_not_breed_another_summary(self, shelf):
        """The summary's moved name is fixed by the book set, so a rerun
        overwrites the summary in place rather than suffixing it further."""
        shelf.stock({"out.mobi": "one two three"})
        shelf.ingest("-t")
        shelf.ingest("-t")
        assert blackbox.tree_of(shelf.outputs) == ["out (2).txt", "out.txt"]


class TestTextModeOverTextAlone:
    """When every input is already a .txt nothing is converted: the originals
    are measured where they are."""

    @pytest.fixture
    def run(self, shelf):
        shelf.stock({"a.txt": "one two three", "sub/b.txt": "one"})
        before = {path: path.read_bytes()
                  for path in shelf.source.rglob("*") if path.is_file()}
        tree = blackbox.tree_of(shelf.source)
        return shelf, shelf.ingest("-t"), before, tree

    def test_the_input_is_byte_identical_afterwards(self, run):
        shelf, _, before, tree = run
        assert blackbox.tree_of(shelf.source) == tree
        assert {path: path.read_bytes() for path in before} == before

    def test_the_output_holds_only_the_summary(self, run):
        shelf, log, _, _ = run
        assert blackbox.tree_of(shelf.outputs) == ["out.txt"]
        assert "Total words across 2 file(s): 4" in log
