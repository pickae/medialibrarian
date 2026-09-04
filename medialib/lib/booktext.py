"""A book to plain text, and the count of it.

The port drives the same converters the bash does rather than reading the
formats itself: poppler's ``pdftotext`` for a PDF, Calibre's ``ebook-convert``
for everything else. A machine without poppler falls through to Calibre exactly
as the bash does, so this is an optimisation and never a new dependency.
"""

import re
import shutil
import subprocess

__all__ = ["book_to_text", "book_to_text_fast", "book_text_counts"]

# The answers the shell gives for a converter that is not there or will not run.
_MISSING = 127
_UNRUNNABLE = 126

_A_NUMBER = re.compile(r"[0-9]+")


def _converter(argv: list[str]) -> int:
    """Run a converter with its output silenced and answer with its status.

    The silence is deliberate, as it is in the bash: the converter's progress
    and warnings are not the caller's to interleave, and the status is the only
    outcome a caller can act on.
    """
    try:
        proc = subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except FileNotFoundError:
        return _MISSING
    except PermissionError:
        return _UNRUNNABLE
    return proc.returncode


def _is_pdf(src: str) -> bool:
    """The bash's ``[[ "${src,,}" == *.pdf ]]``: the suffix, case folded.

    The fold is Python's own rather than the shell's per-character one, and
    that is faithful for the suffix: it is the ASCII ``.pdf`` that is being
    tested, and a code point the two folds treat differently (U+0130) can sit
    in the stem without changing whether the name ENDS with ``.pdf``.
    """
    return src.lower().endswith(".pdf")


def book_to_text(src: str, dest: str) -> int:
    """A book to plain text at ``dest``. The converter's own status.

    A PDF goes through pdftotext, which is the same poppler Calibre's own PDF
    input reaches for and dramatically faster than the e-book pipeline wrapped
    around it; without poppler a PDF still goes to ebook-convert. ``-enc UTF-8``
    is asked for rather than assumed, because the character count that follows
    would be a count of mojibake otherwise.
    """
    if _is_pdf(src) and shutil.which("pdftotext"):
        return _converter(["pdftotext", "-q", "-enc", "UTF-8", "--", src, dest])
    return _converter(["ebook-convert", src, dest])


def book_to_text_fast(src: str, dest: str) -> int:
    """The same, for text that is only measured and then thrown away.

    The difference is PDF and only PDF: ``-nopgbrk`` drops the form feeds
    between pages, which wc treats as word boundaries and nothing downstream
    wants to see. Every other format goes to ``book_to_text`` unchanged - for
    those Calibre IS the fast path, and there is no second tool to prefer.
    """
    if _is_pdf(src) and shutil.which("pdftotext"):
        return _converter(
            ["pdftotext", "-q", "-enc", "UTF-8", "-nopgbrk", "--", src, dest]
        )
    return book_to_text(src, dest)


def book_text_counts(path: str) -> tuple[str, str]:
    """``<words> <characters>`` of a text file; either empty when it cannot be read.

    An empty half is "not counted" rather than a confident 0, which is the only
    distinction a caller has. The counting is wc's own - its definition of a
    word, and its locale-following character count, which is why the file's
    BYTES go to the same wc rather than being counted in Python.

    A host with no ``wc`` is "not counted" as well, rather than an exception
    out of the middle of a census: it is the ONE external tool this package
    reaches for that no preflight asks about, because both callers already
    have somewhere to put an empty answer - the census leaves the words and
    characters columns blank the way it does for a book whose converter is
    missing, and read-library's queue treats an unweighable book as unplaced.
    Counting in Python instead would answer, but with a different definition
    of a word than every other row in the same report was measured with.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return "", ""
    try:
        proc = subprocess.run(
            ["wc", "-w", "-m"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return "", ""
    fields = proc.stdout.decode("ascii", "replace").split()
    words = fields[0] if len(fields) > 0 else ""
    chars = fields[1] if len(fields) > 1 else ""
    if not _A_NUMBER.fullmatch(words):
        words = ""
    if not _A_NUMBER.fullmatch(chars):
        chars = ""
    return words, chars