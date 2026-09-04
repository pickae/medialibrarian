"""`read-library` end to end, against a stubbed narration checkout.

The narration itself is another project's model stack - gigabytes of it, hours per
book - so what is stubbed is exactly that: the checkout's own interpreter, at the
path the setup already accepts as the environment's, so nothing bootstraps a venv.
Everything this command promises is then real.

The stub also stands in for the one behaviour that shapes the narration step: the
engine names its output after the book's METADATA rather than after the file it was
given, so the command has to FIND what appeared instead of expecting a name. And it
refuses a book it cannot open, word for word as the engine does - which is what
makes the relative-path cases below fail on the bug rather than quietly narrate a
path nobody ever looked at.
"""

from __future__ import annotations

import os
import re

import pytest

from tests import blackbox

pytestmark = pytest.mark.stubbed

# Answers the two probes the setup makes, records the arguments app.py was called
# with, and "narrates" - refusing a book it cannot open, which is what a relative
# --ebook produces once app.py is run from inside the checkout.
_CHECKOUT_PYTHON = r"""
if [[ "$1" == "-c" ]]; then
    case "$2" in
        *sysconfig*) mkdir -p "$STUB_SITE"; printf '%s\n' "$STUB_SITE" ;;
        *torch*) [[ -n "${STUB_ENV_UNREADY:-}" ]] && exit 1 ;;
    esac
    exit 0
fi
[[ "$1" == "app.py" ]] || exit 0
shift
printf '%s\n' "$*" >> "$STUB_LOG"
outDir=""; ebook=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --output_dir) outDir="$2"; shift 2 ;;
        --ebook) ebook="$2"; shift 2 ;;
        *) shift ;;
    esac
done
if [[ -n "$ebook" && ! -f "$ebook" ]]; then
    printf 'Error: The provided --ebook %s does not exist.\n' "$ebook"
    exit 1
fi
[[ -n "$outDir" ]] || exit 1
mkdir -p "$outDir"
[[ -n "${STUB_PRODUCES_NOTHING:-}" ]] && exit 0
printf 'audiobook bytes' > "$outDir/Some Title - An Author.m4b"
# The engine's own working directory, exactly where app.py puts it - and left
# behind on purpose, because the engine only clears it by age.
if [[ -n "${STUB_WRITES_MASTER:-}" ]]; then
    proc="$PWD/tmp/proc-$$/deadbeef"
    mkdir -p "$proc/chapters"
    # The master, then a chapter-sized piece assembled on the way - announced the
    # same way in the same log, and announced LAST on purpose. The candidates are
    # walked newest-first, so this is the order in which only the "not a chapter"
    # rule keeps the master from losing to a fragment of itself; with the master
    # last, that rule is never reached and the case cannot fail.
    printf 'the whole book, losslessly' > "$proc/Some Title.flac"
    printf 'Completed \xe2\x86\x92 %s\n' "$proc/Some Title.flac"
    printf 'chapter' > "$proc/chapters/block1.flac"
    printf 'Completed \xe2\x86\x92 %s\n' "$proc/chapters/block1.flac"
fi
# A conversion that died part-way: a file WAS written and the status still says it
# went wrong. Publishing that poisons every later run's resume.
[[ -n "${STUB_DIES_PART_WAY:-}" ]] && exit 1
exit 0
"""

# The shared ffmpeg stub writes an EMPTY file, and this pipeline judges every step
# by whether what came out has bytes in it - an empty audiobook is a failed
# conversion, not a finished book. So this one writes a byte, and records what it
# was asked to do, which is how the cases below can tell WHICH file the lossless
# copy was made from. The "-" guard is the shared stub's: an analysis-only call
# ends in the "-" of "-f null -", which is stdout and not a file to create.
_FFMPEG = r"""
printf '%s\n' "$*" >> "$STUB_FFMPEG_LOG"
out="${!#}"; [[ "$out" == "-" ]] || printf x > "$out"
"""

# The book-to-text step the queue is ordered by, stubbed as a copy - so a
# fixture's word count is simply the words written into it, and the ordering is
# decided here rather than by whether the host has Calibre and poppler.
_EBOOK_CONVERT = r'cat -- "$1" > "$2"'
_PDFTOTEXT = r'out="${!#}"; src="${@:$#-1:1}"; cat -- "$src" > "$out"'


@pytest.fixture
def library(sandbox, tmp_path, private_workspace):
    """A stubbed checkout, the tools around it, and two books.

    The books' CONTENT is what the queue is ordered by, and the two are set
    deliberately at odds with every other order the run could come out in: "plain
    book" is twenty words in 39 bytes, "Der Process" one word in 200. So "plain
    book" is read first if and only if the queue is ordered by WORD COUNT - by
    byte size, or by path, "Der Process" would win both.
    """
    checkout = tmp_path / "e2a"
    (checkout / "python_env" / "bin").mkdir(parents=True)
    (checkout / "app.py").touch()
    interpreter = checkout / "python_env" / "bin" / "python"
    interpreter.write_text("#!/usr/bin/env bash\n%s\n" % _CHECKOUT_PYTHON)
    interpreter.chmod(0o755)

    sandbox.with_media_stubs()
    sandbox.with_tool("ffmpeg", _FFMPEG)
    sandbox.with_tool("jq", "echo 0")
    sandbox.with_tool("rsync", ":")
    sandbox.with_tool("ebook-convert", _EBOOK_CONVERT)
    sandbox.with_tool("pdftotext", _PDFTOTEXT)

    calls = tmp_path / "calls.log"
    ffmpeg_log = tmp_path / "ffmpeg.log"
    calls.write_text("")
    ffmpeg_log.write_text("")

    source = tmp_path / "books"
    (source / "Fiction" / "Kafka").mkdir(parents=True)
    (source / "Fiction" / "Kafka" / "Der Process.epub").write_text("book" * 50)
    (source / "plain book.epub").write_text("a b c d e f g h i j k l m n o p "
                                            "q r s t")
    (source / "cover.jpg").write_text("x")

    base = dict(os.environ, STUB_SITE=str(tmp_path / "site"),
                STUB_LOG=str(calls), STUB_FFMPEG_LOG=str(ffmpeg_log))

    def read(*args, expect=0, cwd=None, **switches):
        done = sandbox.run("read-library", "-e", checkout, "-d", "cpu", *args,
                           cwd=cwd, env=dict(base, **switches), timeout=900)
        assert done.returncode == expect, done.stdout + done.stderr
        return done.stdout + done.stderr

    def engine_calls():
        return [line for line in calls.read_text().splitlines() if line]

    sandbox.checkout = checkout
    sandbox.source = source
    sandbox.outputs = tmp_path / "audiobooks"
    sandbox.calls = calls
    sandbox.engine_calls = engine_calls
    sandbox.ffmpeg_log = ffmpeg_log
    sandbox.scratch = private_workspace
    sandbox.read = read
    return sandbox


class TestAFullRun:
    """Two books, one of them two folders deep, and a file that is not a book."""

    @pytest.fixture
    def run(self, library):
        before = blackbox.tree_of(library.source)
        log = library.read("-l", "deu", library.source, library.outputs,
                           STUB_WRITES_MASTER="1")
        return library, log, before

    def test_both_output_files_land_in_their_own_formats_library(self, run):
        """Under the same name, in the mirrored sub-folder of each."""
        lib, _, _ = run
        assert (lib.outputs / "opus" / "Fiction" / "Kafka"
                / "Der Process.opus").is_file()
        assert (lib.outputs / "flac" / "Fiction" / "Kafka"
                / "Der Process.flac").is_file()
        assert (lib.outputs / "opus" / "plain book.opus").is_file()
        assert (lib.outputs / "flac" / "plain book.flac").is_file()

    def test_nothing_else_is_written_and_the_input_is_untouched(self, run):
        lib, _, before = run
        assert len(list(lib.outputs.rglob("*"))) - len(
            [p for p in lib.outputs.rglob("*") if p.is_dir()]) == 4
        assert blackbox.tree_of(lib.source) == before

    def test_the_lossless_copy_is_made_from_the_whole_book(self, run):
        """Not from one of the chapter-sized pieces assembled on the way, which
        are announced in the very same way in the very same log."""
        lib, _, _ = run
        asked = lib.ffmpeg_log.read_text()
        assert len(re.findall(r"-i \S*/Some Title\.flac .*-c:a flac",
                             asked)) == 2, asked
        assert re.search(r"-i \S*/chapters/", asked) is None, asked

    def test_the_lossless_copy_is_finished_and_not_merely_copied(self, run):
        """The same loudness normalisation the engine put into the m4b, so the
        file kept beside the audiobook sounds like the audiobook."""
        lib, _, _ = run
        assert len(re.findall(r"-af dynaudnorm",
                             lib.ffmpeg_log.read_text())) == 2

    def test_the_engines_work_directory_is_cleaned_up(self, run):
        """It holds a FLAC of the whole book plus one per chapter and one per
        sentence, and the engine only clears it by age."""
        lib, _, _ = run
        assert list((lib.checkout / "tmp").glob("proc-*")) == []

    @pytest.mark.parametrize("argument", ["--headless", "--output_format m4b",
                                          "--output_channel mono",
                                          "--language deu"])
    def test_the_four_decisions_this_command_makes_reach_the_engine(self, run,
                                                                   argument):
        lib, _, _ = run
        assert len([c for c in lib.engine_calls() if argument in c]) == 2

    def test_there_is_one_call_per_book_and_only_per_book(self, run):
        lib, _, _ = run
        assert len(lib.engine_calls()) == 2

    def test_the_queue_reads_the_longest_book_first(self, run):
        """So one huge book cannot land last and hold the device on its own after
        everything else has drained. Reading on a CPU is one book at a time, so
        the log's order IS the queue's."""
        lib, _, _ = run
        calls = lib.engine_calls()
        assert "plain book.epub" in calls[0], calls
        assert "Der Process.epub" in calls[-1], calls

    def test_each_book_gets_its_own_output_directory(self, run):
        lib, _, _ = run
        assert all("--output_dir /" in call for call in lib.engine_calls())

    @pytest.mark.parametrize("row", ["Books found:       2", "Read:              2",
                                     "Real-time speedup:"])
    def test_the_closing_statistics_say_what_happened(self, run, row):
        _, log, _ = run
        assert row in log, log

    def test_a_books_two_console_lines_carry_the_same_number(self, run):
        """So a start can be paired with its finish in a console where several
        books interleave."""
        _, log, _ = run
        assert "[1/2] Reading (deu)" in log, log
        assert "[1/2] Done (flac + opus)" in log, log

    def test_the_ram_scratch_is_handed_back(self, run):
        lib, _, _ = run
        assert list(lib.scratch.iterdir()) == []


class TestASecondRun:
    def test_nothing_is_narrated_again_and_no_output_is_rewritten(self, library):
        library.read("-l", "deu", library.source, library.outputs,
                     STUB_WRITES_MASTER="1")
        before = sorted((str(p), p.stat().st_size)
                        for p in library.outputs.rglob("*") if p.is_file())
        library.calls.write_text("")
        log = library.read("-l", "deu", library.source, library.outputs,
                           STUB_WRITES_MASTER="1")
        assert library.engine_calls() == []
        assert sorted((str(p), p.stat().st_size)
                      for p in library.outputs.rglob("*") if p.is_file()) \
            == before
        assert "Skipped (done):    2" in log, log


class TestAConversionThatLeavesNoLosslessMaster:
    """The engine's own m4b is kept alongside the Opus - still a generation better
    than the Opus made from it - in a library of its own name, so a folder never
    claims anything the files in it are not."""

    def test_the_m4b_is_kept_and_there_is_no_flac_library_at_all(self, library):
        library.read("-l", "deu", library.source, library.outputs)
        assert (library.outputs / "opus" / "plain book.opus").is_file()
        assert (library.outputs / "m4b" / "plain book.m4b").is_file()
        assert not (library.outputs / "flac").exists()


class TestAskingForOneLibraryOnly:
    def test_the_listening_copy_alone(self, library):
        library.read("-l", "deu", "-o", library.source, library.outputs,
                     STUB_WRITES_MASTER="1")
        assert (library.outputs / "opus" / "plain book.opus").is_file()
        assert sorted(p.name for p in library.outputs.iterdir()) == ["opus"]

    def test_the_lossless_file_alone(self, library):
        library.read("-l", "deu", "-b", "0", library.source, library.outputs,
                     STUB_WRITES_MASTER="1")
        assert (library.outputs / "flac" / "plain book.flac").is_file()
        assert list(library.outputs.rglob("*.opus")) == []

    def test_asking_for_neither_is_refused_and_creates_nothing(self, library,
                                                               tmp_path):
        nothing = tmp_path / "out.nothing"
        library.read("-b", "0", "-o", library.source, nothing, expect=1)
        assert not nothing.exists()


class TestABookThatProducesNothing:
    """The engine exits 0 on some conversions that wrote no file at all, which is
    why the FILE decides success. A library holds the odd DRM-locked or malformed
    file and one of them must not end a run."""

    def test_it_is_reported_and_the_run_carries_on(self, library):
        log = library.read("-l", "deu", library.source, library.outputs,
                           STUB_PRODUCES_NOTHING="1")
        assert "Failed:            2" in log, log
        assert list(library.outputs.rglob("*.opus")) == []


class TestANarrationThatDiedPartWay:
    """A file WAS written and the engine still failed. It must not be published:
    an unlistenable audiobook at the expected path is a book every later run then
    skips."""

    def test_it_is_reported_failed_and_nothing_is_published(self, library):
        log = library.read("-l", "deu", library.source, library.outputs,
                           STUB_DIES_PART_WAY="1")
        assert "Failed:            2" in log, log
        assert [p for p in library.outputs.rglob("*") if p.is_file()] == []


class TestACheckoutWhoseEnvironmentIsNotPopulatedYet:
    """The first book is read on its own, because that book's run is also the
    checkout's dependency install and two of those at once corrupt the
    environment. Every book still gets read.

    `-j 2` on purpose: the device decides the job count and this fixture reads on
    a CPU, which is always one book at a time.
    """

    @pytest.fixture
    def run(self, library):
        log = library.read("-j", "2", "-l", "deu", library.source,
                           library.outputs, STUB_ENV_UNREADY="1")
        return library, log

    def test_it_says_the_first_book_is_read_on_its_own(self, run):
        _, log = run
        assert "reading the first book on its own" in log, log

    def test_every_book_is_still_read_exactly_once(self, run):
        lib, _ = run
        assert len(lib.engine_calls()) == 2
        assert len(list(lib.outputs.rglob("*.opus"))) == 2

    def test_no_staging_file_is_left_behind(self, run):
        lib, _ = run
        assert list(lib.outputs.rglob(".readLibrary.*")) == []


class TestOneVoicePerLanguage:
    """A directory of samples is one voice per language."""

    @pytest.fixture
    def voices(self, library, tmp_path):
        folder = tmp_path / "voices"
        folder.mkdir()
        for name in ("deu.wav", "english.wav", "default.wav",
                     "notalanguage.wav"):
            (folder / name).write_text("RIFF")
        return library, folder

    def test_a_book_gets_the_sample_of_its_own_language(self, voices):
        library, folder = voices
        log = library.read("-l", "deu", "-v", folder, library.source,
                           library.outputs)
        assert len([c for c in library.engine_calls()
                    if re.search(r"--voice \S*/deu/", c)]) == 2
        assert "one per language" in log, log

    def test_a_sample_naming_no_language_is_named_and_ignored(self, voices):
        library, folder = voices
        log = library.read("-l", "deu", "-v", folder, library.source,
                           library.outputs)
        assert 'notalanguage.wav" names no language' in log, log

    def test_a_language_with_no_sample_of_its_own_falls_back(self, voices):
        library, folder = voices
        library.read("-l", "ita", "-v", folder, library.source, library.outputs)
        assert len([c for c in library.engine_calls()
                    if re.search(r"--voice \S*/-/", c)]) == 2


class TestTheRefusalsThatComeBeforeAnyWork:
    def test_a_language_the_engine_cannot_speak(self, library, tmp_path):
        target = tmp_path / "out.swe"
        log = library.read("-l", "swe", library.source, target, expect=1)
        assert "it speaks" in log, log
        assert not target.exists()

    def test_a_missing_checkout_names_what_to_clone(self, sandbox, library,
                                                   tmp_path):
        done = sandbox.run("read-library", "-e", tmp_path / "nowhere",
                           library.source, tmp_path / "out.none")
        log = done.stdout + done.stderr
        assert done.returncode == 1, log
        assert "ebook2audiobook" in log, log

    def test_a_job_count_below_one(self, library, tmp_path):
        library.read("-j", "0", library.source, tmp_path / "out.jobs", expect=1)


class TestRelativeDirectories:
    """The narration does not happen in the directory the run was started in: the
    engine is driven from INSIDE its checkout, so a relative `--ebook` is looked
    for under that checkout and every book reported missing.

    The output side is the same question, and the answer must be the directory the
    run was started in.
    """

    def test_a_relative_input_and_output_are_resolved_against_the_caller(
            self, library, tmp_path):
        log = library.read("-l", "deu", "books", "audiobooks", cwd=tmp_path)
        assert "does not exist" not in log, log
        assert "Read:              2" in log, log

    def test_the_engine_is_given_an_absolute_book_path(self, library, tmp_path):
        """Which is the fix: it means the same thing from inside the checkout as
        it did on the command line."""
        library.read("-l", "deu", "books", "audiobooks", cwd=tmp_path)
        assert len([c for c in library.engine_calls()
                    if "--ebook /" in c]) == 2

    def test_the_output_lands_where_the_caller_asked_for_it(self, library,
                                                            tmp_path):
        library.read("-l", "deu", "books", "audiobooks", cwd=tmp_path)
        assert (tmp_path / "audiobooks" / "opus" / "plain book.opus").is_file()
        assert (tmp_path / "audiobooks" / "opus" / "Fiction" / "Kafka"
                / "Der Process.opus").is_file()
        assert not (library.checkout / "audiobooks").exists()

    def test_a_path_reaching_its_library_through_dot_dot(self, library,
                                                          tmp_path):
        """The same question the other way round - which a path merely prefixed
        with the working directory would still get right, and which a resolved one
        keeps right in the output tree as well."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        library.read("-l", "deu", "../books", "../audiobooks.two",
                     cwd=elsewhere)
        assert len([c for c in library.engine_calls()
                    if "--ebook /" in c]) == 2
        assert (tmp_path / "audiobooks.two" / "opus"
                / "plain book.opus").is_file()
        assert not (elsewhere / "audiobooks.two").exists()

    def test_the_nested_output_guard_sees_through_a_relative_pairing(
            self, library, tmp_path):
        library.read("-l", "deu", "books", "books/out", cwd=tmp_path, expect=1)
        assert not (library.source / "out").exists()
