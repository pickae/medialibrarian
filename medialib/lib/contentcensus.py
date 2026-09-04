"""What a file IS, and its report row.

``content-census`` is the command around it; this is the part that decides which
report a file belongs in and renders one row for it. The two halves of the actual
probing live beside it, split the way the tools do: ``censusmedia`` reads audio
and video with ffprobe and mediainfo, ``censusdocuments`` reads books and comics
with Calibre, poppler, the archive extractors and ImageMagick.

The classification is by suffix, except for a PDF. A PDF is in the comic list AND
in the book list, because it genuinely is both in the wild: a scanned comic and a
text e-book arrive in the same container. That judgement is made once in
``comicpdf`` - every page holds exactly one image, drawn across nearly the whole
page - and it is made here by the same call the comic conversion gates on, so both
agree about every file. Its numbers are kept for the row builder that follows,
which needs the page count the probe already read: one probe answers both
questions.
"""

import contextlib
import io
import os
import shutil

from medialib.lib import censusdocuments, censusmedia, comicpdf, enums
from medialib.lib.enums import lower_extension_of, shell_lower

__all__ = [
    "census_init",
    "census_union",
    "census_classify",
    "census_row",
]

# 7-Zip's binary has three names in the wild - 7z from p7zip, 7zz upstream, 7za in
# the reduced package - so the one this host has is resolved once.
_SEVENZIP_NAMES = ("7z", "7zz", "7za")


def census_union(extensions):
    """The given extensions, lower-cased, de-duplicated, in the order first seen -
    one space-separated string, which is the shape the central lists have.

    The accumulator is a STRING and not a list, because the shell's is: membership
    is tested by splitting it on whitespace, so an EMPTY word is never a member of
    it and every empty word takes the "not seen yet" branch again. Appending
    nothing to a non-empty string still appends the separator, so ``jpg ""``
    answers "jpg " and ``jpg "" png`` answers "jpg  png" - which a list-and-join
    would have tidied into something the shell never says.
    """
    out = ""
    for extension in extensions:
        extension = shell_lower(extension)
        if extension in out.split():
            continue
        out += (" " if out else "") + extension
    return out


def census_video_extensions():
    """``videoExtensions`` (convertAudio's video inputs) plus
    ``sourceVideoExtensions`` (ingestMovies' loose video), because a library holds
    both kinds and neither list is a superset of the other: .ts and .wmv are only
    in the first, .vob and .m3u8 only in the second."""
    return census_union(list(enums.VIDEO_EXTENSIONS)
                        + list(enums.SOURCE_VIDEO_EXTENSIONS))


def census_page_extensions():
    """What counts as a PAGE inside a comic archive: the image and cover lists -
    between them every scan format in the wild - plus avif, which is what the
    comic and image conversions EMIT, so a converted library censuses as one
    instead of as a stack of archives holding no pages."""
    return census_union(list(enums.IMAGE_EXTENSIONS)
                        + list(enums.COVER_IMAGE_EXTENSIONS) + ["avif"])


def census_all_extensions():
    return census_union(list(enums.AUDIO_EXTENSIONS)
                        + census_video_extensions().split()
                        + list(enums.COMIC_EXTENSIONS)
                        + list(enums.COMIC_PDF_EXTENSIONS)
                        + list(enums.BOOK_INPUT_EXTENSIONS))


def census_init():
    """Settle the derived enums and what this host can actually probe, once, so no
    row builder asks the same question per file - and so the caller can say up
    front which columns are going to stay empty.

    These are the OPTIONAL tools: their absence costs a column, and the run says
    so and goes on. The ones whose absence would cost a whole content type are
    asked for through ``requireTools`` by the command instead.

    The settled values are left in the environment the way the shell's own
    assignments leave them, so the row builders read one answer rather than
    probing again.
    """
    os.environ["censusVideoExtensions"] = census_video_extensions()
    os.environ["censusPageExtensions"] = census_page_extensions()
    os.environ["censusAllExtensions"] = census_all_extensions()

    os.environ["CENSUS_HAVE_MEDIAINFO"] = (
        "1" if shutil.which("mediainfo") else "")
    # Whether the three tools that decide what a PDF IS are all here.
    os.environ["CENSUS_HAVE_POPPLER"] = (
        "1" if _missing_pdf_tools() == "" else "")
    os.environ["CENSUS_HAVE_EBOOK_CONVERT"] = (
        "1" if shutil.which("ebook-convert") else "")
    # Separate from CENSUS_HAVE_POPPLER: this one is about reading a PDF's TEXT,
    # and it is what makes a PDF's word count affordable - so a host with poppler
    # but without Calibre can still count its PDFs, which is why the two are asked
    # separately rather than folded into one flag.
    os.environ["CENSUS_HAVE_PDFTOTEXT"] = (
        "1" if shutil.which("pdftotext") else "")

    sevenzip = ""
    for candidate in _SEVENZIP_NAMES:
        if shutil.which(candidate):
            sevenzip = candidate
            break
    os.environ["CENSUS_SEVENZIP"] = sevenzip
    return 0


def _missing_pdf_tools():
    """What ``comicPdfMissingTools`` prints - the tools it cannot find, or
    nothing. The shell reads it with ``[[ -z "$(...)" ]]``, so what matters is
    only whether anything was printed."""
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        comicpdf.comic_pdf_missing_tools()
    return captured.getvalue().strip()


def census_classify(path):
    """Which report this file belongs in - "audio", "video", "books", "comics",
    or "" for a suffix no list claims - and the PDF probe's numbers alongside it,
    as ``(type, stats)``.

    Both are returned together for the reason the shell leaves them in globals: a
    caller that took only the type would have to run the probe again to get the
    page count the row builder needs.

    Without poppler the PDF question cannot be asked at all, and every PDF is
    counted as a book - the more conservative half, since a book row's page count
    is the only thing that then goes missing, whereas a comic row would be
    inventing a container and a page resolution.
    """
    extension = lower_extension_of(path)
    if not extension:
        return "", ""

    comic_pdf = os.environ.get(
        "comicPdfExtensions", " ".join(enums.COMIC_PDF_EXTENSIONS)).split()
    if extension in [shell_lower(e) for e in comic_pdf]:
        if not os.environ.get("CENSUS_HAVE_POPPLER", ""):
            return "books", ""
        # isComicPdf PRINTS its stats whichever way it votes, precisely so one
        # probe answers both questions; the shell captures that print.
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            status = comicpdf.is_comic_pdf(path)
        stats = captured.getvalue().rstrip("\n")
        return ("comics" if status == 0 else "books"), stats

    # The video list is the one censusInit derives, read from where it settled it
    # the way censusdocuments reads censusPageExtensions - so a caller that
    # settled something else is honoured, and one that never called censusInit
    # still gets the answer the module would have given.
    video = os.environ.get("censusVideoExtensions",
                           census_video_extensions()).split()
    for names, content in (
            (enums.AUDIO_EXTENSIONS, "audio"),
            (video, "video"),
            (enums.COMIC_EXTENSIONS, "comics"),
            (enums.BOOK_INPUT_EXTENSIONS, "books")):
        if extension in [shell_lower(e) for e in names]:
            return content, ""
    return "", ""


def census_row(content, path, separator=None):
    """That file's data row, as ``(row, reason)`` - the reason instead of a row
    when the file cannot be censused as what its suffix claims, which is the one
    thing the caller reports per file."""
    if content == "audio":
        return censusmedia.census_audio_row(path, separator)
    if content == "video":
        return censusmedia.census_video_row(path, separator)
    if content == "books":
        return censusdocuments.census_book_row(path)
    if content == "comics":
        return censusdocuments.census_comic_row(path)
    return None, 'no census knows what a "%s" is' % content
