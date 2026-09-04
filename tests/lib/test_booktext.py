"""The white box for medialib/lib/booktext.py.

The converters are stubs that record their argv to a file, so "which tool was
reached for, with what" is an assertion rather than a guess; and the counting
is the real wc, so only what locales agree on is pinned here - word counts and
ASCII character counts exactly, the multibyte cases under the suite's pinned
C.UTF-8, which is what the module hands the same bytes to on any host.

One input is not something two coreutils agree on at all: an invalid byte is one
character to GNU wc and none to uutils. What the module PROMISES about it is
that the file's own bytes reach wc and that wc's two numbers come back in the
right order, so that case compares against this host's wc rather than against a
number recorded from one of them.
"""

import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from medialib.lib import booktext

pytestmark = pytest.mark.stubbed

_STUB = (
    "#!/usr/bin/env bash\n"
    'printf "%s\\0" "$0" "$@" >> "$RECORD"\n'
    'exit "${STUB_EXIT:-0}"\n'
)


@pytest.fixture()
def converters(tmp_path, monkeypatch):
    """A PATH holding only the named recording stubs.

    Narrow rather than prepended: a host that happens to have a real pdftotext
    would otherwise answer for the stub in the "absent" cases. The bash symlink
    is the stub's shebang.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bash").symlink_to(shutil.which("bash"))
    record = tmp_path / "calls"
    installed = []

    def install(name):
        path = bin_dir / name
        path.write_text(_STUB)
        os.chmod(path, 0o755)
        installed.append(name)

    def calls():
        if not record.exists():
            return []
        return [part.decode("utf-8")
                for part in record.read_bytes().split(b"\0") if part]

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("RECORD", str(record))
    monkeypatch.setenv("STUB_EXIT", "0")
    return SimpleNamespace(install=install, calls=calls, bin_dir=bin_dir)


@pytest.mark.parametrize("name", ["book.pdf", "BOOK.PDF", "Book.PdF", "İ.book.pdf"])
def test_pdf_goes_to_pdftotext_with_its_flags(converters, name):
    converters.install("pdftotext")
    converters.install("ebook-convert")
    status = booktext.book_to_text(f"/library/{name}", "/out/text.txt")
    assert status == 0
    calls = converters.calls()
    assert calls[0] == str(converters.bin_dir / "pdftotext")
    assert calls[1:] == ["-q", "-enc", "UTF-8", "--", f"/library/{name}", "/out/text.txt"]


@pytest.mark.parametrize("name", ["book.epub", "book.mobi", "noext", "",
                                  "book.pdf.bak", "book.PDF ", "pdf",
                                  # the routing reads the FILE's extension: a
                                  # shelf called "scanned.pdfs" holds epubs
                                  "scanned.pdfs/collected.epub"])
def test_everything_else_goes_to_ebook_convert(converters, name):
    converters.install("pdftotext")
    converters.install("ebook-convert")
    status = booktext.book_to_text(f"/library/{name}", "/out/text.txt")
    assert status == 0
    calls = converters.calls()
    assert calls[0] == str(converters.bin_dir / "ebook-convert")
    assert calls[1:] == [f"/library/{name}", "/out/text.txt"]


def test_without_pdftotext_a_pdf_falls_back(converters):
    converters.install("ebook-convert")
    status = booktext.book_to_text("/library/book.pdf", "/out/text.txt")
    assert status == 0
    assert converters.calls()[0].endswith("/ebook-convert")


def test_a_converter_that_is_not_there_answers_127(converters):
    status = booktext.book_to_text("/library/book.epub", "/out/text.txt")
    assert status == 127
    assert converters.calls() == []


def test_a_refusing_converter_answers_its_own_status(converters, monkeypatch):
    converters.install("pdftotext")
    monkeypatch.setenv("STUB_EXIT", "7")
    status = booktext.book_to_text("/library/book.pdf", "/out/text.txt")
    assert status == 7


def test_fast_pass_adds_nopgbrk_for_pdf_only(converters):
    converters.install("pdftotext")
    converters.install("ebook-convert")
    assert booktext.book_to_text_fast("/library/book.pdf", "/out/t.txt") == 0
    calls = converters.calls()
    assert calls[0] == str(converters.bin_dir / "pdftotext")
    assert calls[1:] == ["-q", "-enc", "UTF-8", "-nopgbrk", "--",
                         "/library/book.pdf", "/out/t.txt"]

    converters.bin_dir.parent.joinpath("calls").unlink()
    assert booktext.book_to_text_fast("/library/book.epub", "/out/t.txt") == 0
    calls = converters.calls()
    assert calls[0] == str(converters.bin_dir / "ebook-convert")
    assert calls[1:] == ["/library/book.epub", "/out/t.txt"]


def test_counts_of_ascii_text(tmp_path):
    target = tmp_path / "text.txt"
    target.write_bytes(b"one two three\n")
    assert booktext.book_text_counts(str(target)) == ("3", "14")


def test_counts_without_a_trailing_break(tmp_path):
    target = tmp_path / "text.txt"
    target.write_bytes(b"no trailing newline")
    assert booktext.book_text_counts(str(target)) == ("3", "19")


def test_counts_of_an_empty_file(tmp_path):
    target = tmp_path / "text.txt"
    target.write_bytes(b"")
    assert booktext.book_text_counts(str(target)) == ("0", "0")


def test_counts_of_a_file_that_is_not_there(tmp_path):
    assert booktext.book_text_counts(str(tmp_path / "missing.txt")) == ("", "")


def test_counts_on_a_host_with_no_wc(tmp_path, monkeypatch):
    """The one external tool here that no preflight asks about, so its absence
    has to arrive as an answer rather than as an exception: it used to come out
    of the middle of a census as a FileNotFoundError.

    "Not counted" is what both callers already handle - the census leaves the
    two columns blank, and read-library's queue treats the book as unplaced.
    """
    def absent(*_args, **_kwargs):
        raise FileNotFoundError(2, "no such file", "wc")

    target = tmp_path / "text.txt"
    target.write_bytes(b"one two three\n")
    monkeypatch.setattr(booktext.subprocess, "run", absent)
    assert booktext.book_text_counts(str(target)) == ("", "")


def test_a_file_that_cannot_be_read_never_reaches_wc(tmp_path, monkeypatch):
    """The two empty answers are not the same empty answer, and only one of
    them is worth starting a process for."""
    started = []
    monkeypatch.setattr(booktext.subprocess, "run",
                        lambda *a, **k: started.append(a))
    assert booktext.book_text_counts(str(tmp_path / "missing.txt")) == ("", "")
    assert started == []


def test_counts_of_multibyte_text_counts_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    target = tmp_path / "text.txt"
    target.write_bytes("Fünf Wörter\n".encode())
    assert booktext.book_text_counts(str(target)) == ("2", "12")


def _host_wc(data: bytes) -> tuple[str, str]:
    """What this host's own `wc -w -m` makes of these bytes."""
    done = subprocess.run(["wc", "-w", "-m"], input=data,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    fields = done.stdout.decode("ascii", "replace").split()
    return fields[0], fields[1]


def test_counts_of_invalid_utf8(tmp_path, monkeypatch):
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    data = b"\xff\xfe x\n"
    target = tmp_path / "text.txt"
    target.write_bytes(data)
    got = booktext.book_text_counts(str(target))
    # The bytes go through unchanged and the two numbers come back in wc's own
    # order. Neither number is pinned: these bytes are "2 3" to uutils and
    # "1 5" to GNU, and which is right belongs to the host's wc.
    assert got == _host_wc(data)
    # And the case is still about invalid bytes rather than about nothing: both
    # halves came back counted, whichever flavor did the counting.
    assert all(half.isdigit() for half in got)
