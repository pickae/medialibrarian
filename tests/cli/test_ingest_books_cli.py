"""`ingest-books` end to end: a tree of sources in, one book each out.

Every heavy tool is a stub, so no real books and no converter are involved. What
that leaves is the control flow no helper's own cases reach: the mirrored tree,
which sources take the unpack-clean-repack path and which do not, the collision
suffix, and the progress counter.

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
    """The stubs, and a nested input tree covering every source kind."""
    sandbox.with_tool("ebook-convert", _EBOOK_CONVERT)
    sandbox.with_tool("gs", _GHOSTSCRIPT)
    sandbox.with_tool("unzip", _UNZIP)
    sandbox.with_tool("zip", _ZIP)
    sandbox.with_tool("convert", _CONVERT)

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

    def run():
        done = sandbox.run("ingest-books", source, outputs, timeout=600)
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout + done.stderr

    sandbox.source = source
    sandbox.outputs = outputs
    sandbox.ingest = run
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
