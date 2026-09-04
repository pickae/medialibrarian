"""`convert-comics` as a process: a tree of comic archives, one .cbz out of each.

The whole pipeline runs for real - pretreat, extract, `convert-images`,
`clean-folder-structure -n`, package - with only the two genuinely heavy
externals stood in for: ImageMagick, so no codecs are needed, and `fdupes`, whose
real self would delete one book's identical stub pages as duplicates of another's
and change the very counts under test. `zip` and `unzip` are the real ones,
because the archives are the thing being made.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import zipfile

import pytest

from tests import blackbox

pytestmark = pytest.mark.stubbed

# Fixed dimensions for every image, original and trimmed alike, so the trim finds
# nothing worth taking and the plain convert path is the one exercised.
_IDENTIFY_FLAT = 'echo "1000 1500"'

# Write the output - the last argument, minus a trailing ">" and a leading
# "miff:" for the in-memory trim target - starting with a line naming the page it
# came from, then padded past the 10k below which a page reads as a broken encode.
_CONVERT_STAMPING = '''
src=""
for a in "$@"; do [[ -f "$a" ]] && { src="$a"; break; }; done
out="${!#}"; out="${out%\\>}"; out="${out#miff:}"
{ printf "AVIF from %s\\n" "${src##*/}"; head -c 12288 /dev/zero | tr "\\0" "a"; } > "$out"
exit 0
'''


def _cbz(directory, name, pages):
    """A real .cbz, built the way a user's is."""
    build = directory / ("build_" + name.replace("/", "_"))
    for page, text in pages.items():
        full = build / page
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)
    target = directory / name
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["zip", "-q", "-r", str(target.resolve()), "."],
                   cwd=build, check=True)
    shutil.rmtree(build)
    return target


def _entries(archive):
    with zipfile.ZipFile(archive) as opened:
        return sorted(opened.namelist())


def _first_line(archive, entry):
    with zipfile.ZipFile(archive) as opened:
        return opened.read(entry).split(b"\n", 1)[0].decode()


@pytest.fixture
def comics(sandbox, tmp_path):
    """The real `zip` and `unzip`, and an input and output folder.

    `fdupes` is deliberately NOT installed here: a case that wants it neutral
    stubs it, and the case that is about its absence needs a PATH without it.
    """
    for tool in ("zip", "unzip"):
        if not shutil.which(tool):
            pytest.fail("%s is missing: the archives are what is under test"
                        % tool)
    sandbox.inputs = tmp_path / "in"
    sandbox.outputs = tmp_path / "out"
    sandbox.inputs.mkdir()
    return sandbox


class TestThePackaging:
    """The AVIF pages are intermediate: nothing but a .cbz reaches the disk, and
    the output tree has one folder level LESS than a folder-of-pages layout."""

    @pytest.fixture
    def run(self, comics):
        comics.with_tool("identify", _IDENTIFY_FLAT)
        comics.with_tool("convert", _CONVERT_STAMPING)
        comics.with_tool("fdupes", "exit 0")
        _cbz(comics.inputs, "Alpha.cbz",
             {"01.jpg": "alpha 1", "02.jpg": "alpha 2", "03.jpg": "alpha 3"})
        # One folder deeper, with a nested page and a non-image, so the
        # flattening and the pruning run for real on the way to packaging.
        _cbz(comics.inputs, "Series/Charlie.cbz",
             {"a.jpg": "charlie a", "b.jpg": "charlie b",
              "nested/c.jpg": "charlie c", "notes.txt": "not an image"})
        before = blackbox.tree_of(comics.inputs)
        done = comics.run("convert-comics", comics.inputs, comics.outputs)
        assert done.returncode == 0, done.stdout + done.stderr
        return comics, done.stdout + done.stderr, before

    def test_the_output_mirrors_the_input_one_cbz_per_archive(self, run):
        comics, _, _ = run
        assert blackbox.tree_of(comics.outputs) == [
            "Alpha.cbz", "Series", "Series/Charlie.cbz"]

    def test_a_book_becomes_a_file_and_not_a_folder(self, run):
        comics, _, _ = run
        assert not (comics.outputs / "Alpha").exists()
        assert not (comics.outputs / "Series" / "Charlie").exists()

    def test_the_input_is_left_exactly_as_it_was(self, run):
        comics, _, before = run
        assert blackbox.tree_of(comics.inputs) == before

    def test_each_book_announces_itself_once_by_its_full_path(self, run):
        """Before it is converted rather than after, and by path: a library has
        many same-named files in different series folders, so a bare filename
        would not say which book a worker picked up."""
        comics, log, _ = run
        assert len(re.findall(r"Converting: ", log)) == 2
        assert "Converting: %s" % (comics.inputs / "Series" / "Charlie.cbz") in log

    def test_the_counter_reaches_the_book_total_exactly_once(self, run):
        """The outcome lines a started book can still print are notes, not
        counted lines."""
        _, log, _ = run
        assert len(re.findall(r"\[2/2\] ", log)) == 1

    def test_each_archive_holds_that_books_numbered_pages(self, run):
        comics, _, _ = run
        assert _entries(comics.outputs / "Alpha.cbz") == [
            "1.avif", "2.avif", "3.avif"]
        assert _entries(comics.outputs / "Series" / "Charlie.cbz") == [
            "1.avif", "2.avif", "3.avif"]

    def test_the_pages_are_stored_rather_than_deflated(self, run):
        """AVIF does not compress further, and some readers page through an
        archive as stored."""
        comics, _, _ = run
        with zipfile.ZipFile(comics.outputs / "Alpha.cbz") as opened:
            assert [info.filename for info in opened.infolist()] == [
                "1.avif", "2.avif", "3.avif"]
            assert {info.compress_type for info in opened.infolist()} == {
                zipfile.ZIP_STORED}

    def test_the_nested_page_was_flattened_into_the_last_slot(self, run):
        comics, _, _ = run
        charlie = comics.outputs / "Series" / "Charlie.cbz"
        assert _first_line(charlie, "1.avif") == "AVIF from a.jpg"
        assert _first_line(charlie, "3.avif") == "AVIF from c.jpg"

    def test_a_second_run_recognises_the_finished_archives(self, run):
        """No rewrite, and above all no " (2)" duplicate of every book."""
        comics, _, _ = run
        before = blackbox.tree_of(comics.outputs)
        again = comics.run("convert-comics", comics.inputs, comics.outputs)
        assert again.returncode == 0, again.stderr
        assert blackbox.tree_of(comics.outputs) == before
        assert "Skip (exists)" in again.stdout + again.stderr

    def test_no_ram_work_directory_is_left_behind(self, run):
        """Including the per-book folders a worker frees as soon as its book is
        zipped. The base is this test's own, so anything in it is this run's."""
        import os
        base = os.environ["comicsRamBase"]
        assert blackbox.tree_of(base) == []


class TestThePageStatistics:
    """The closing report's page total, its seconds per page, and the share each
    kind of page ended in.

    Those numbers cross a process boundary, which is why they need a whole run:
    one book is converted per child, each child appends its counters to a shared
    file, and both RAM work trees are gone by the time the report is printed - so
    nothing can be recounted from the files afterwards.

    The stubs are rigged so ONE run produces every category, the page's name
    deciding what `identify` reports for its trim:

      *-plain.jpg   trims to nothing         -> converted
      *-trim.jpg    trims 4% off each edge   -> trimmed
      *-blank.jpg   trims away past 99%      -> blank, dropped by convert-images
      *-broken.jpg  converts to a 1k page    -> converted, then dropped here
    """

    # The page's original size is always 1000x1500; what its TRIM came out as
    # depends on the page name, which the convert stub stamps into the in-memory
    # trim file so this one can look it up. Against convert-images' thresholds
    # (1% minimum, 20% maximum, 99% blank) 960x1460 is a margin worth taking and
    # 5x5 is a blank page.
    _IDENTIFY = '''
target="${!#}"; target="${target#miff:}"
name="${target##*/}"
if [[ "$name" == trimmed_* ]]; then
    name="$(head -1 "$target" 2>/dev/null)"
else
    echo "1000 1500"; exit 0
fi
case "$name" in
    *-trim.jpg)  echo "960 1460" ;;
    *-blank.jpg) echo "5 5" ;;
    *)           echo "1000 1500" ;;
esac
'''

    # Three jobs in one, as ImageMagick's own is: asked for "info:" it samples the
    # corner colour and lightness - a light margin, so a trimmable page really is
    # trimmed - and otherwise it writes the output stamped with the page it came
    # from. A *-broken.jpg is written far below the minimum page size, which is
    # how a truncated encode presents itself.
    _CONVERT = '''
for a in "$@"; do [[ "$a" == "info:" ]] && { echo "white 100"; exit 0; }; done
src=""
for a in "$@"; do p="${a#miff:}"; [[ -f "$p" ]] && { src="$p"; break; }; done
out="${!#}"; out="${out%\\>}"; out="${out#miff:}"
name="${src##*/}"
[[ "$name" == trimmed_* ]] && name="$(head -1 "$src")"
size=12288
[[ "$name" == *-broken.jpg ]] && size=1024
{ printf "%s\\n" "$name"; head -c "$size" /dev/zero | tr "\\0" a; } > "$out"
'''

    @pytest.fixture
    def run(self, comics):
        comics.with_tool("identify", self._IDENTIFY)
        comics.with_tool("convert", self._CONVERT)
        comics.with_tool("fdupes", "exit 0")
        # The two names share no affix on purpose: the naming pass strips what
        # siblings have in common, so "Mixed Pages"/"Clean Pages" would package
        # as "Mixed.cbz"/"Clean.cbz" and these assertions would be about naming.
        _cbz(comics.inputs, "Mixture.cbz",
             {"01-plain.jpg": "a", "02-trim.jpg": "b",
              "03-blank.jpg": "c", "04-broken.jpg": "d"})
        _cbz(comics.inputs, "Uniform.cbz",
             {"01-plain.jpg": "e", "02-plain.jpg": "f"})
        done = comics.run("convert-comics", comics.inputs, comics.outputs)
        assert done.returncode == 0, done.stdout + done.stderr
        return comics, done.stdout + done.stderr

    def test_the_page_total_the_books_and_a_per_page_time(self, run):
        """The time itself is a measurement; that a per-page figure is given at
        all is the claim."""
        _, log = run
        assert re.search(
            r"^6 page\(s\) in 2 converted book\(s\), \d+\.\d+ seconds per page$",
            log, re.M), log

    def test_one_share_line_per_category_that_occurred(self, run):
        """4 converted (two plain in one book, two in the other, the broken one
        among them), 1 trimmed, 1 blank - and the broken page reported again as
        the subset of "converted" that did not survive, since the encoder that
        wrote it counted it as converted."""
        _, log = run
        shares = [re.sub(r"\s+", " ", line).strip() for line in log.splitlines()
                  if re.match(r"^    .*: *\d+/\d+ \(\d+\.?\d*%\)$", line)]
        assert shares == [
            "converted: 4/6 (66.7%)",
            "trimmed: 1/6 (16.7%)",
            "blank, dropped: 1/6 (16.7%)",
            "of those broken, dropped: 1/6 (16.7%)"]

    def test_a_category_nothing_landed_in_is_left_out(self, run):
        """A run of ordinary comics must not carry lines of zeroes."""
        _, log = run
        assert "already done, skipped" not in log
        assert "not found, failed" not in log

    def test_the_partitioning_categories_add_up_to_the_total(self, run):
        """The broken line is deliberately not in this sum."""
        _, log = run
        counted = sum(int(n) for n in re.findall(
            r"^    (?:converted|trimmed|blank, dropped|already done, skipped"
            r"|not found, failed): *(\d+)/", log, re.M))
        assert counted == 6

    def test_the_archives_hold_exactly_the_pages_the_report_kept(self, run):
        """The blank page was dropped by the encoder and the broken one here,
        and both books are packaged either way."""
        comics, _ = run
        assert _entries(comics.outputs / "Mixture.cbz") == ["1.avif", "2.avif"]
        assert _entries(comics.outputs / "Uniform.cbz") == ["1.avif", "2.avif"]

    def test_and_they_are_the_two_that_should_have_survived(self, run):
        comics, _ = run
        mixture = comics.outputs / "Mixture.cbz"
        assert _first_line(mixture, "1.avif") == "01-plain.jpg"
        assert _first_line(mixture, "2.avif") == "02-trim.jpg"

    def test_a_rerun_that_converts_nothing_prints_no_page_section(self, run):
        """"0 page(s)" with a share of nothing would be worse than none."""
        comics, _ = run
        again = comics.run("convert-comics", comics.inputs, comics.outputs)
        assert again.returncode == 0, again.stderr
        log = again.stdout + again.stderr
        assert "Converting:" not in log
        assert "page(s) in" not in log
        assert not [line for line in log.splitlines()
                    if re.match(r"^    .*: *\d+/\d+ \(\d+\.?\d*%\)$", line)]


class TestStreamingBookByBook:
    """The RAM ceiling, which is invisible in the output.

    A worker unpacks one book, converts it, zips it to disk and frees that book's
    two RAM folders before the next is unpacked into them - so peak RAM is
    (workers x one book) rather than (the whole collection). That is what makes a
    thousand-archive run survivable, and the packaging assertions above cannot
    see it at all.

    The fixture is sized from the host's pool: the queue has to be LONGER than the
    pool for any of this to be observable, because with fewer books than workers
    every book is in flight at once and no second batch ever waits for a first to
    free its RAM.
    """

    @staticmethod
    def _pool():
        """The same arithmetic the command does: slots are the thread count
        oversubscribed by two over the four threads a page conversion takes, then
        divided by the four pages a book converts at once."""
        import os as _os
        threads = _os.cpu_count() or 1
        slots = max(1, threads * 2 // 4)
        return max(2, slots // 4)

    @pytest.fixture
    def run(self, comics, tmp_path):
        import os as _os
        import threading
        import time

        comics.with_tool("identify", _IDENTIFY_FLAT)
        comics.with_tool("convert", _CONVERT_STAMPING)
        comics.with_tool("fdupes", "exit 0")
        workers = self._pool()
        books = workers * 2 + 2
        for number in range(1, books + 1):
            _cbz(comics.inputs, "Weekly Comic - Issue %d.cbz" % number,
                 {"0%d.jpg" % page: "book %d page %d" % (number, page)
                  for page in (1, 2, 3)})

        base = _os.environ["comicsRamBase"]
        samples = []
        stop = threading.Event()

        def watch():
            """How many book folders exist across both work trees, and how many
            archives are already on disk. A book folder is a directory directly
            inside a `convertComics.*` tree - matched by shape rather than by
            depth, the run having a directory of its own in the base."""
            from pathlib import Path
            while not stop.is_set():
                resident = [path for tree in Path(base).glob("**/convertComics.*")
                            for path in tree.iterdir()
                            if path.is_dir() and not path.name.startswith(".pack.")]
                on_disk = list(comics.outputs.rglob("*.cbz")) \
                    if comics.outputs.exists() else []
                samples.append((len(resident), len(on_disk)))
                time.sleep(0.02)

        sampler = threading.Thread(target=watch, daemon=True)
        sampler.start()
        try:
            done = comics.run("convert-comics", comics.inputs, comics.outputs)
        finally:
            stop.set()
            sampler.join(timeout=5)
        assert done.returncode == 0, done.stdout + done.stderr
        return comics, samples, workers, books

    def test_every_book_in_one_archive_each_out(self, run):
        comics, _, _, books = run
        assert len(list(comics.outputs.rglob("*.cbz"))) == books

    def test_the_books_resident_at_once_stay_within_the_pool(self, run):
        """Two folders per book at most - its pages and its AVIFs - so the peak
        is bounded by twice the pool. The fixture holds more than twice the
        pool's worth of books, so keeping every book resident would blow
        straight through it."""
        _, samples, workers, _ = run
        peak = max((resident for resident, _ in samples), default=0)
        assert peak <= 2 * workers, "peak %d, pool %d" % (peak, workers)

    def test_and_the_sampler_did_see_something(self, run):
        """A sampler that observed nothing would make the bound above pass for
        the wrong reason."""
        _, samples, _, _ = run
        assert max((resident for resident, _ in samples), default=0) >= 1

    def test_an_archive_is_on_disk_while_another_book_is_still_in_ram(self, run):
        """If packaging only began once every book was converted, no sample could
        show both at once."""
        _, samples, _, _ = run
        assert [1 for resident, on_disk in samples if resident and on_disk]

    def test_nothing_is_left_in_the_ram_base(self, run):
        import os as _os
        assert blackbox.tree_of(_os.environ["comicsRamBase"]) == []

    def test_the_collective_naming_pass_still_ran(self, run):
        """Every book was alone in RAM, so the shared prefix can only have been
        stripped by the second pass over the finished archives on disk."""
        comics, _, _, books = run
        names = sorted(p.name for p in comics.outputs.glob("*.cbz"))
        assert len(names) == books
        assert not [n for n in names if n.startswith("Weekly Comic - ")]
        assert len(set(names)) == books

    def test_the_pages_inside_are_still_numbered_per_book(self, run):
        """The numbering happens in the worker; the second pass must not have
        disturbed it."""
        comics, _, _, _ = run
        first = sorted(comics.outputs.glob("*.cbz"))[0]
        assert _entries(first) == ["1.avif", "2.avif", "3.avif"]

    def test_a_rerun_skips_every_book_by_the_input_it_recorded(self, run):
        """A book's NAME depends on which other books were in the run, so a
        resume check on names alone is not enough - each archive records the
        input it came from in its zip comment, and that is what is matched."""
        comics, _, _, books = run
        before = blackbox.tree_of(comics.outputs)
        again = comics.run("convert-comics", comics.inputs, comics.outputs)
        assert again.returncode == 0, again.stderr
        log = again.stdout + again.stderr
        assert blackbox.tree_of(comics.outputs) == before
        assert log.count("Skip (exists)") == books
        assert "Converting:" not in log

        first = sorted(comics.outputs.glob("*.cbz"))[0]
        with zipfile.ZipFile(first) as opened:
            assert opened.comment.decode().strip().endswith(".cbz")

    def test_a_library_that_has_grown_keeps_the_names_it_already_had(self, run):
        """Adding a book that shares nothing with the others changes what the
        collective rules strip, so this run would call the converted books
        something else than they are called on disk. The alternative to
        recognising them is a second copy of every book in the library."""
        comics, _, _, books = run
        existing = sorted(p.name for p in comics.outputs.glob("*.cbz"))
        _cbz(comics.inputs, "Special Annual 2024.cbz",
             {"0%d.jpg" % page: "annual %d" % page for page in (1, 2, 3)})

        grown = comics.run("convert-comics", comics.inputs, comics.outputs)
        assert grown.returncode == 0, grown.stderr
        log = grown.stdout + grown.stderr
        assert log.count("Converting:") == 1
        assert log.count("Skip (exists)") == books
        assert len(list(comics.outputs.rglob("*.cbz"))) == books + 1
        assert [name for name in existing
                if not (comics.outputs / name).exists()] == []


class TestWithoutFdupes:
    """`fdupes` de-duplicates the input before a run - identical files converted
    once and stored as hard links rather than as separate copies - so its absence
    is a startup warning and not a refusal.

    The state is settled once and exported, and the per-book `convert-images`
    children inherit it, so the fact is said once per run rather than once per
    book. Both halves work on any host: the first FORCES the state and puts a
    bomb on PATH, so a broken guard would be a failed run; the second builds a
    PATH that cannot find fdupes at all, even where it is installed, and asserts
    the settlement itself.
    """

    # What a run reaches for beyond its own package. Symlinked rather than
    # inherited, because the point is a PATH that holds these and no fdupes.
    _ORDINARY = ("bash", "head", "tr", "mktemp", "rmdir", "rm", "find", "cp",
                 "mv", "zip", "unzip", "sort", "flock")

    def test_a_forced_absent_state_skips_the_call_without_warning(
            self, comics, tmp_path):
        """The bomb exits non-zero, so a guard that called it anyway would fail
        the run - "exits 0" IS the assertion that the call was skipped. And a
        state handed down is not re-settled, so nothing warns."""
        comics.with_tool("identify", _IDENTIFY_FLAT)
        comics.with_tool("convert", _CONVERT_STAMPING)
        comics.with_tool("fdupes", "exit 1")
        _cbz(comics.inputs, "Alpha.cbz",
             {"0%d.jpg" % page: "alpha %d" % page for page in (1, 2, 3)})

        import os as _os
        env = dict(_os.environ, HAVE_FDUDES="")
        done = comics.run("convert-comics", comics.inputs, comics.outputs,
                          env=env)
        assert done.returncode == 0, done.stdout + done.stderr
        assert _entries(comics.outputs / "Alpha.cbz") == [
            "1.avif", "2.avif", "3.avif"]
        assert "fdupes not found" not in done.stdout + done.stderr

    def test_a_run_that_cannot_find_it_warns_once_for_the_whole_run(
            self, comics, tmp_path):
        """Once, parent and every per-book child together: the parent settles it
        and exports the answer, and a child has nothing to add to something the
        run has already said."""
        comics.with_tool("identify", _IDENTIFY_FLAT)
        comics.with_tool("convert", _CONVERT_STAMPING)
        comics.linking(*self._ORDINARY).narrow()
        assert shutil.which("fdupes", path=comics.path) is None

        _cbz(comics.inputs, "Alpha.cbz",
             {"0%d.jpg" % page: "alpha %d" % page for page in (1, 2, 3)})
        _cbz(comics.inputs, "Series/Beta.cbz",
             {"0%d.jpg" % page: "beta %d" % page for page in (1, 2)})

        import os as _os
        env = {key: value for key, value in _os.environ.items()
               # The suite-wide off switch would skip the very settlement under
               # test.
               if key != "SKIP_TOOL_PREFLIGHT"}
        done = comics.run("convert-comics", comics.inputs, comics.outputs,
                          env=env)
        log = done.stdout + done.stderr
        assert done.returncode == 0, log
        assert (comics.outputs / "Alpha.cbz").is_file()
        assert (comics.outputs / "Series" / "Beta.cbz").is_file()
        assert log.count("fdupes not found") == 1
        assert log.count("conversion itself is unaffected") == 1


# What each PDF "contains" is dictated by two files named after it in $pdfMeta:
# <name>.info is what pdfinfo prints, <name>.images what `pdfimages -list` does.
# They live OUTSIDE the input tree, so the input holds nothing but the books.
_PDFINFO = '''
pdf="${!#}"
meta="$pdfMeta/${pdf##*/}.info"
[[ -f "$meta" ]] || { echo "pdfinfo: cannot read" >&2; exit 1; }
cat "$meta"
'''
_PDFIMAGES = '''
pdf="${!#}"
meta="$pdfMeta/${pdf##*/}.images"
[[ -f "$meta" ]] || { echo "pdfimages: cannot read" >&2; exit 1; }
cat "$meta"
'''
# One file per page, and a log of the arguments, so the resolution the pipeline
# chose can be asserted.
_PDFTOPPM = '''
printf "%s\\n" "$*" >> "$PDFTOPPM_LOG"
root="${!#}"; pdf="${@:$#-1:1}"
pages=$(awk "/^Pages:/ { print \\$2; exit }" "$pdfMeta/${pdf##*/}.info")
for ((p = 1; p <= pages; p++)); do printf "page %s\\n" "$p" > "$root-$p.jpg"; done
'''

_IMAGES_HEADER = (
    "page   num  type   width height color comp bpc  enc interp  object ID"
    " x-ppi y-ppi size ratio\n"
    + "-" * 91 + "\n")


def _pdf(meta_dir, path, pages, images_per_page):
    """A PDF the poppler stubs will describe. One image covering the page at 300
    ppi is a scanned comic; four small ones strewn over it is a layout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    info = ["Pages:          %d\n" % pages]
    listing = [_IMAGES_HEADER]
    for page in range(1, pages + 1):
        info.append("Page %5d size: 612 x 792 pts (letter)\nPage %5d rot:  0\n"
                    % (page, page))
        width, height = (2550, 3300) if images_per_page == 1 else (600, 450)
        size = "400K" if images_per_page == 1 else "120K"
        for _ in range(images_per_page):
            listing.append(
                "%5d %5d image   %5d %5d  rgb     3   8  jpeg   no        12"
                "  0   300   300  %s 3.6%%\n" % (page, 0, width, height, size))
    (meta_dir / (path.name + ".info")).write_text("".join(info))
    (meta_dir / (path.name + ".images")).write_text("".join(listing))
    return path


def _source_of(archive):
    """The book an archive came from, out of its zip comment. The reliable way
    to say WHICH book an archive is: the names are decided collectively and may
    have been cleaned."""
    with zipfile.ZipFile(archive) as opened:
        return opened.comment.decode().strip()


class TestComicsThatArriveAsPdfs:
    """The container, and the judgement that comes with it. The archive path is
    covered above; what a PDF adds is that it might not be a comic at all."""

    @pytest.fixture
    def run(self, comics, tmp_path):
        import os as _os

        comics.with_tool("identify", _IDENTIFY_FLAT)
        comics.with_tool("convert", _CONVERT_STAMPING)
        comics.with_tool("fdupes", "exit 0")
        comics.with_tool("pdfinfo", _PDFINFO)
        comics.with_tool("pdfimages", _PDFIMAGES)
        comics.with_tool("pdftoppm", _PDFTOPPM)

        meta = tmp_path / "pdfMeta"
        meta.mkdir()
        rendered = tmp_path / "pdftoppm.log"
        rendered.write_text("")

        (comics.inputs / "Series").mkdir(parents=True)
        (comics.inputs / "Both").mkdir(parents=True)
        _cbz(comics.inputs, "Alpha.cbz",
             {"0%d.jpg" % p: "page %d" % p for p in (1, 2, 3)})
        _pdf(meta, comics.inputs / "Series" / "Scanned Comic.pdf", 3, 1)
        _pdf(meta, comics.inputs / "Series" / "Weekly Magazine.pdf", 6, 4)
        # The same book in both containers, in one folder.
        _cbz(comics.inputs, "Both/Batman.cbz",
             {"0%d.jpg" % p: "page %d" % p for p in (1, 2)})
        _pdf(meta, comics.inputs / "Both" / "Batman.pdf", 2, 1)

        comics.meta = meta
        comics.rendered = rendered
        comics.env = dict(_os.environ, pdfMeta=str(meta),
                          PDFTOPPM_LOG=str(rendered))
        before = blackbox.tree_of(comics.inputs)
        done = comics.run("convert-comics", comics.inputs, comics.outputs,
                          env=comics.env)
        assert done.returncode == 0, done.stdout + done.stderr
        return comics, done.stdout + done.stderr, before

    def test_the_magazine_is_recognised_and_refused(self, run):
        _, log, _ = run
        assert "Not a comic, skipped: Series/Weekly Magazine.pdf" in log
        assert "Comic: Series/Scanned Comic.pdf" in log
        assert "2 of 3 PDF(s) taken as comic book(s)" in log

    def test_one_archive_per_book_and_none_for_the_magazine(self, run):
        comics, _, _ = run
        files = [p for p in comics.outputs.rglob("*") if p.is_file()]
        assert len(files) == 4
        assert all(p.suffix == ".cbz" for p in files)
        assert len(list(comics.outputs.glob("Series/*.cbz"))) == 1

    def test_each_archive_records_the_book_it_came_from(self, run):
        comics, _, _ = run
        assert sorted(_source_of(p) for p in comics.outputs.rglob("*.cbz")) == [
            "Alpha.cbz", "Both/Batman.cbz", "Both/Batman.pdf",
            "Series/Scanned Comic.pdf"]

    def test_one_book_in_two_containers_gives_two_archives(self, run):
        """They would otherwise both want the same output name and one book
        would be silently dropped. The second keeps the " (N)" suffix this repo
        gives any collision - not the mangled name the collective rules would
        produce if they had been shown two siblings sharing a prefix."""
        comics, _, _ = run
        both = comics.outputs / "Both"
        assert sorted(p.name for p in both.glob("*.cbz")) == [
            "Batman (2).cbz", "Batman.cbz"]
        assert _source_of(both / "Batman.cbz") == "Both/Batman.cbz"
        assert _source_of(both / "Batman (2).cbz") == "Both/Batman.pdf"
        assert len(_entries(both / "Batman (2).cbz")) == 2

    def test_the_pdfs_pages_are_the_rendered_ones(self, run):
        comics, _, _ = run
        archive = next(iter(comics.outputs.glob("Series/*.cbz")))
        assert _entries(archive) == ["1.avif", "2.avif", "3.avif"]
        assert _first_line(archive, "1.avif") == "AVIF from page-1.jpg"
        assert _first_line(archive, "3.avif") == "AVIF from page-3.jpg"
        with zipfile.ZipFile(archive) as opened:
            assert {info.compress_type for info in opened.infolist()} == {
                zipfile.ZIP_STORED}

    def test_rendered_at_the_capped_native_resolution(self, run):
        """The fixture's pages are drawn at 300 ppi, and the default 2960 px cap
        over an 11 inch page is 270 dpi - so the cap decides here, and no page is
        rendered larger than the pipeline keeps."""
        comics, _, _ = run
        log = comics.rendered.read_text()
        assert len([line for line in log.splitlines() if "-r 270" in line]) == 2
        assert "Weekly Magazine" not in log

    def test_the_input_is_left_exactly_as_it_was(self, run):
        comics, _, before = run
        assert blackbox.tree_of(comics.inputs) == before

    def test_a_second_run_converts_nothing_and_refuses_the_magazine_again(
            self, run):
        comics, _, _ = run
        before = blackbox.tree_of(comics.outputs)
        again = comics.run("convert-comics", comics.inputs, comics.outputs,
                           env=comics.env)
        assert again.returncode == 0, again.stderr
        log = again.stdout + again.stderr
        assert blackbox.tree_of(comics.outputs) == before
        assert log.count("Skip (exists)") == 4
        assert "Converting:" not in log
        assert log.count(
            "Not a comic, skipped: Series/Weekly Magazine.pdf") == 1

    def test_nothing_is_left_in_the_ram_base(self, run):
        import os as _os
        assert blackbox.tree_of(_os.environ["comicsRamBase"]) == []

    def test_a_folder_of_magazines_is_refused_rather_than_emptied(self, run,
                                                                  tmp_path):
        """The case where the extension promised a book and the content did not:
        it must say so instead of writing an empty output tree."""
        comics, _, _ = run
        magazines = tmp_path / "mags"
        magazines.mkdir()
        _pdf(comics.meta, magazines / "Vogue.pdf", 4, 4)
        _pdf(comics.meta, magazines / "Time.pdf", 4, 4)

        done = comics.run("convert-comics", magazines, tmp_path / "magsOut",
                          env=comics.env)
        assert done.returncode == 1
        assert "none of the 2 PDF(s)" in done.stdout + done.stderr
        assert not (tmp_path / "magsOut" / "Vogue.cbz").exists()

    def test_an_output_inside_the_input_is_refused(self, run):
        """A .cbz written into the source tree is exactly what the next run would
        pick up as a book, and the de-duplication pass over the input would reach
        into the finished library first."""
        comics, _, before = run
        inside = comics.run("convert-comics", comics.inputs,
                            comics.inputs / "out", env=comics.env)
        assert inside.returncode == 1
        assert "Refusing to write the output inside the input" in (
            inside.stdout + inside.stderr)
        assert not (comics.inputs / "out").exists()

        same = comics.run("convert-comics", comics.inputs, comics.inputs,
                          env=comics.env)
        assert same.returncode == 1
        assert blackbox.tree_of(comics.inputs) == before
