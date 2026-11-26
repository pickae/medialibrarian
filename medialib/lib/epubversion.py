"""What an epub holds that an EPUB 2 file cannot.

EPUB 2 is what the most readers open, so it is what Calibre writes by default
and what ingest-books writes when nothing is lost by it. Two kinds of book are
the exception:

* **content EPUB 2 has no place for** - MathML, ``<audio>`` and ``<video>``.
  Calibre keeps that markup whichever version it writes, but in an EPUB 2 it
  makes an invalid file that a strict reader may refuse; written as EPUB 3, the
  same book is valid and the readers that render it are told it is there.
* **what Calibre's conversion drops in either version** - a fixed layout and
  read-aloud media overlays. No ``--epub-version`` keeps those, so such a book
  is not to be re-converted at all.

Only the package document and the pages are read, straight from the zip. A file
that is not a readable zip asks for nothing: it goes the way every book went
before this existed.
"""

import re
import zipfile

__all__ = ["calibre_drops", "needs_epub3"]

# The most of one member that is read: far beyond any real page, and short of
# what a zip bomb's single member would expand to.
_MEMBER_CAP = 64 * 1024 * 1024

_PAGE_SUFFIXES = (".xhtml", ".html", ".htm")

# By element, with or without a namespace prefix (``<m:math``).
_EPUB3_CONTENT = (
    ("MathML", re.compile(rb"<(?:[\w-]+:)?math[\s>/]")),
    ("audio", re.compile(rb"<(?:[\w-]+:)?audio[\s>/]")),
    ("video", re.compile(rb"<(?:[\w-]+:)?video[\s>/]")),
)

_FIXED_LAYOUT = re.compile(
    rb"""property\s*=\s*["']rendition:layout["'][^>]*>\s*pre-paginated"""
    rb"""|rendition:layout-pre-paginated""")
_MEDIA_OVERLAY = re.compile(rb"""\smedia-overlay\s*=\s*["'][^"']+["']""")


def _members(epub: str, suffixes: tuple[str, ...]):
    """The contents of every member whose name ends in one of ``suffixes``;
    nothing at all for a file that is not a readable zip."""
    try:
        with zipfile.ZipFile(epub) as packed:
            for info in packed.infolist():
                if info.is_dir() or not info.filename.lower().endswith(suffixes):
                    continue
                with packed.open(info) as member:
                    yield member.read(_MEMBER_CAP)
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError):
        return


def needs_epub3(epub: str) -> list[str]:
    """What ``epub`` holds that only an EPUB 3 carries, by name ("MathML",
    "audio", "video"), in that order; empty when EPUB 2 loses nothing."""
    found: set[str] = set()
    for page in _members(epub, _PAGE_SUFFIXES):
        found.update(name for name, pattern in _EPUB3_CONTENT
                     if name not in found and pattern.search(page))
        if len(found) == len(_EPUB3_CONTENT):
            break
    return [name for name, _ in _EPUB3_CONTENT if name in found]


def calibre_drops(epub: str) -> list[str]:
    """What re-converting ``epub`` with Calibre would lose in either EPUB
    version ("fixed layout", "read-aloud"); empty when nothing."""
    found = []
    for package in _members(epub, (".opf",)):
        if "fixed layout" not in found and _FIXED_LAYOUT.search(package):
            found.append("fixed layout")
        if "read-aloud" not in found and _MEDIA_OVERLAY.search(package):
            found.append("read-aloud")
    return found
