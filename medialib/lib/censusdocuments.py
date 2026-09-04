"""The books and comics census rows.

Two content types, measured on completely different things. A book is measured on
its TEXT, which means converting it first (``booktext``), because a word count of
an epub's XHTML would count markup and of a mobi's record stream would count
nothing; a .txt is already that text and is counted where it lies. A comic is
measured on its PAGES, which means reading the archive's index and decoding
exactly ONE member - the first page in natural sort order, the order the comic
conversion numbers pages in - to a pipe, never to disk.

Both row builders answer with a row, or with a reason the file could not be read
as what its suffix claims. The shell leaves those in ``CENSUS_ROW`` and
``CENSUS_SKIP_REASON`` because its caller must not run the builders in a
subshell; here they are the return value, and the caller's own globals are the
environment the way the shell's exports are.
"""

import os
import subprocess

from medialib.lib import census, imagemagick
from medialib.lib.booktext import book_text_counts, book_to_text
from medialib.lib.enums import lower_extension_of, shell_lower
from medialib.lib.formatting import awk_number
from medialib.lib.versionsort import version_sorted

__all__ = [
    "census_book_row",
    "census_comic_row",
    "census_comic_pdf_row",
    "census_comic_archive_row",
    "census_first_page",
    "census_zip_pattern",
    "extension_list",
]

# The suffix lists the caller settles (censusInit) and this module only reads.
# The defaults are what that init computes on a stock checkout, so a caller that
# never ran it still gets the answers the module would have given.
_COMIC_PDF_EXTENSIONS = "pdf"
_PAGE_EXTENSIONS = "jpg jpeg webp png svg tiff tif bmp avif"

# The scratch file the text conversion writes, rewritten per book and never kept.
_TEXT_SCRATCH = "censusBook.txt"


def _config(name, default):
    return os.environ.get(name, default)


def _flag(name):
    """A CENSUS_HAVE_* answer, which the shell tests with ``[[ -n ]]``."""
    return os.environ.get(name, "") != ""


def _extensions(name, default):
    return _config(name, default).split()


def extension_list(extensions):
    """``"jpg jpeg png"`` -> ``".jpg / .jpeg / .png"`` - the shell's
    ``extensionList``, which the no-page skip reason spells the looked-for
    suffixes with."""
    out = ""
    for extension in extensions:
        out += (" / " if out else "") + "." + extension
    return out


def _first_word(text):
    """The first whitespace-separated word, the way ``read -r first _ _`` takes
    it off a here-string: an empty string when there is none."""
    fields = text.split()
    return fields[0] if fields else ""


def _capture(argv):
    """A tool's stdout WHATEVER it exited with - the shell's ``tool | ...`` and
    its ``tool ... || true``.

    Both of those discard the status deliberately: a pipeline's status is its
    last stage's, and the ``|| true`` says so outright. pdfimages exiting
    non-zero on a PDF it still described, and an extractor that wrote the page
    and then complained, are both read for what they printed. An absent tool is
    the empty answer, which is the shell's 127 with nothing on stdout.
    """
    try:
        proc = subprocess.run(list(argv), stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL)
    except (OSError, ValueError):
        return b""
    return proc.stdout


def _capture_or_empty(argv):
    """A tool's stdout, or b"" when it failed - the shell's
    ``out="$(tool ... 2>/dev/null)" || out=""``, where the status is what decides
    whether the archive opened at all."""
    try:
        proc = subprocess.run(list(argv), stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL)
    except (OSError, ValueError):
        return b""
    if proc.returncode != 0:
        return b""
    return proc.stdout


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# --- books ---------------------------------------------------------------------


def census_book_row(file, log=None):
    """The books report's row for <file> - path, size, pages, words, characters -
    or ``(None, reason)`` for a file that cannot be read as what its suffix
    claims.

    Pages are a PDF-only column, because only a PDF has any; the number is the
    one the classifier's pdfinfo probe already read, so no PDF is opened twice.
    Words and characters come from the text conversion, and stay EMPTY rather
    than skipping the row when the tool that would read this format is not
    installed: the path and size of every book in the library are worth having on
    their own.
    """
    extension = lower_extension_of(file)
    comic_pdf = _extensions("comicPdfExtensions", _COMIC_PDF_EXTENSIONS)
    pages = ""
    words = ""
    chars = ""
    can_convert = False

    if census.extension_in(extension, comic_pdf):
        if _flag("CENSUS_HAVE_POPPLER"):
            pages = census.to_int(_first_word(_config("CENSUS_PDF_STATS", "")))
            if pages == "" or pages == "0":
                return None, "pdfinfo could not read it as a PDF"

    if census.extension_in(extension, comic_pdf):
        # A PDF is convertible when EITHER tool is installed: pdftotext reads a
        # PDF and ebook-convert reads everything else, so a host with poppler and
        # no Calibre counts its PDFs and leaves only its epubs and mobis blank.
        can_convert = (_flag("CENSUS_HAVE_PDFTOTEXT")
                       or _flag("CENSUS_HAVE_EBOOK_CONVERT"))
    else:
        can_convert = _flag("CENSUS_HAVE_EBOOK_CONVERT")

    if extension == "txt":
        words, chars = book_text_counts(file)
    elif can_convert:
        scratch = _config("CENSUS_SCRATCH", "") or _config("TMPDIR", "") or "/tmp"
        text_file = os.path.join(scratch, _TEXT_SCRATCH)
        _remove(text_file)
        if book_to_text(file, text_file) != 0:
            _remove(text_file)
            return None, ("its text could not be read (it may be encrypted or "
                          "truncated)")
        words, chars = book_text_counts(text_file)
        _remove(text_file)

    words = census.to_int(words)
    chars = census.to_int(chars)
    row = census.join([file, census.file_size(file), pages, words, chars],
                      _config("CENSUS_SEP", census.DEFAULT_SEPARATOR))
    return row, None


# --- comics --------------------------------------------------------------------


def census_comic_row(file, log=None):
    """The comics report's row for <file> - path, size, pages, first page
    resolution, container, image codec. Two shapes of comic, one row: an archive
    and a PDF holding one image per page, and the container column says which."""
    extension = lower_extension_of(file)
    if census.extension_in(extension,
                           _extensions("comicPdfExtensions",
                                       _COMIC_PDF_EXTENSIONS)):
        return census_comic_pdf_row(file)
    return census_comic_archive_row(file, extension, log)


def census_comic_pdf_row(file):
    """The comics row for a comic PDF. The page count is the one the classifier
    already read; the first page's pixel size and encoding come from pdfimages'
    listing of page 1 alone."""
    pages = census.to_int(_first_word(_config("CENSUS_PDF_STATS", "")))
    resolution, codec = _pdf_first_page(file)
    row = census.join([file, census.file_size(file), pages, resolution, "pdf",
                       codec],
                      _config("CENSUS_SEP", census.DEFAULT_SEPARATOR))
    return row, None


def _pdf_first_page(file):
    """pdfimages' row for page 1, as ``(WxH, codec)``.

    The awk it stands for takes the FIRST row whose first field is a number and
    whose type is image or stencil - a "smask" row is that image's alpha channel,
    not a second image - and prints the width and height as integers with
    poppler's name for the stream filter, lowercased. Anything else, including a
    tool that is not there, leaves both empty.
    """
    listing = _capture(["pdfimages", "-list", "-f", "1", "-l", "1", file])
    text = listing.decode("utf-8", "replace").replace("\r", "")
    for line in text.split("\n"):
        fields = line.split()
        if len(fields) < 9:
            continue
        if not fields[0].isdigit():
            continue
        if fields[2] not in ("image", "stencil"):
            continue
        # awk's %d, which TRUNCATES toward zero - not the shell printf's %.0f,
        # which rounds half to even. A width of "12.7" is 12 columns here.
        width = int(awk_number(fields[3]))
        height = int(awk_number(fields[4]))
        return "%dx%d" % (width, height), shell_lower(fields[8])
    return "", ""


# Which extractor each container's suffix asks for, and the name of the container
# the row records. An archive that will not open with its own extractor is
# skipped: a .cbz that is really a RAR would otherwise be recorded with a
# container it does not have.
_CONTAINERS = {"cbz": "zip", "cbr": "rar", "cb7": "7z"}


def _archive_listing(file, extension):
    """The archive's member names, one per line, or "" when it would not open."""
    if extension == "cbz":
        return _capture_or_empty(["unzip", "-Z1", "--", file])
    if extension == "cbr":
        return _capture_or_empty(["unrar", "lb", file])
    if extension == "cb7":
        sevenzip = _config("CENSUS_SEVENZIP", "")
        if not sevenzip:
            return b""
        out = _capture_or_empty([sevenzip, "l", "-ba", "-slt", file])
        kept = []
        for line in out.decode("utf-8", "replace").split("\n"):
            if line.startswith("Path = "):
                kept.append(line[len("Path = "):])
        return ("\n".join(kept) + "\n").encode("utf-8") if kept else b""
    return b""


def census_comic_archive_row(file, extension, log=None):
    """The comics row for a .cbz/.cbr/.cb7."""
    if log is None:
        def log(_message):
            return None
    container = _CONTAINERS.get(extension, "")
    listing = _archive_listing(file, extension)
    if not listing:
        return None, "it could not be opened as a %s archive" % container

    page_extensions = _extensions("censusPageExtensions", _PAGE_EXTENSIONS)
    pages = 0
    first_page = ""
    # The image members, in the order the pages would be numbered in: sort -V is
    # the natural sort the pages are numbered with, so "9" precedes "10".
    text = listing.decode("utf-8", "replace")
    names = text.split("\n")
    if names and names[-1] == "":
        names.pop()
    for member in version_sorted(names):
        if not member:
            continue
        member_extension = member.rpartition("/")[2]
        dot = member_extension.rfind(".")
        # ?*.* - a name whose only dot is its first character has no suffix
        if dot < 1:
            continue
        member_extension = member_extension[dot + 1:]
        if not census.extension_in(shell_lower(member_extension),
                                   page_extensions):
            continue
        pages += 1
        if not first_page:
            first_page = member

    if pages == 0:
        return None, ("it holds no image page (looked for %s)"
                      % extension_list(page_extensions))

    # The one page that is decoded. An archive whose first page ImageMagick
    # cannot read is NOT skipped - its page count and container are facts - but
    # the two columns that stay empty are said out loud, because an empty cell
    # nobody mentioned reads like a tool that was never run.
    codec, resolution = _identify(file, extension, first_page)
    if not resolution:
        log('  WARNING: "%s": its first page (%s) could not be read, so' % (
            file, first_page))
        log("           the page resolution and image codec columns stay empty")

    row = census.join([file, census.file_size(file), str(pages), resolution,
                       container, shell_lower(codec)],
                      _config("CENSUS_SEP", census.DEFAULT_SEPARATOR))
    return row, None


def _identify(file, extension, member):
    """ImageMagick's format and pixel size for that one member, as
    ``(codec, WxH)``.

    The format is ``%m %wx%h\\n`` - a backslash and an n, because the shell
    writes it inside single quotes and it is identify that turns them into a line
    break. Passing a real newline would be a different argument.

    The member is extracted to a PIPE, so nothing is decoded past the first page
    and nothing is written into the library being censused. The carriage returns
    come off because a CR that rode along from a tool built for CRLF would end up
    inside the resolution column - and, being a line break, would then have to be
    quoted or replaced by the row's own joiner.
    """
    page = census_first_page(file, extension, member)
    try:
        proc = subprocess.run(
            imagemagick.identify_argv(["-format", "%m %wx%h\\n", "-"]),
            input=page, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
    except (OSError, ValueError):
        return "", ""
    text = proc.stdout.decode("utf-8", "replace").replace("\r", "")
    line = text.split("\n")[0] if text else ""
    fields = line.split()
    codec = fields[0] if fields else ""
    resolution = fields[1] if len(fields) > 1 else ""
    return codec, resolution


def census_first_page(file, extension, member):
    """That ONE archive member's bytes. Extracted to a pipe, never to disk:
    ImageMagick reads the header and answers from it."""
    if extension == "cbz":
        return _capture(["unzip", "-p", "--", file, census_zip_pattern(member)])
    if extension == "cbr":
        return _capture(["unrar", "p", "-inul", file, member])
    if extension == "cb7":
        sevenzip = _config("CENSUS_SEVENZIP", "")
        if not sevenzip:
            return b""
        return _capture([sevenzip, "x", "-so", file, member])
    return b""


def census_zip_pattern(name):
    """An archive member name turned into an unzip pattern that matches only
    itself.

    unzip has no "literal name" option - every name on its command line is a
    shell pattern - so a page called "01 [scan].jpg" would be asked for as a
    character class and not found. The three metacharacters are each wrapped in a
    one-element class, which is unzip's own way of quoting them, and "[" goes
    first because the replacements for "*" and "?" introduce brackets of their
    own.
    """
    name = name.replace("[", "[[]")
    name = name.replace("*", "[*]")
    name = name.replace("?", "[?]")
    return name
