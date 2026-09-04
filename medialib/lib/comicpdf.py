"""A comic book that arrives as a PDF.

The port drives the same poppler tools the bash does: ``pdfinfo`` for the page
count and the page sizes, ``pdfimages -list`` for what each page holds and how
large its image is drawn, ``pdftoppm`` for the render. The judgement itself -
one image per page, drawn across the page - is the awk program the bash feeds
the tools' combined output to, reimplemented line for line below.
"""

import os
import re
import shutil
import subprocess

from medialib.lib.formatting import awk_number

__all__ = [
    "comic_pdf_stats",
    "is_comic_pdf",
    "render_comic_pdf_pages",
    "comic_pdf_missing_tools",
    "comic_pdf_has_pages",
]

# The answers the shell gives for a tool that is not there or will not run.
_MISSING = 127
_UNRUNNABLE = 126

_A_NUMBER = re.compile(r"[0-9]+")
_PAGE = re.compile(r"^Page +[0-9]+ +size:")


def _knob(name: str, default: str) -> float:
    """An exported knob as awk's ``-v`` would read it: ``value + 0``."""
    return awk_number(os.environ.get(name, default))


def _run(argv: list[str]) -> int:
    """Run a tool with its output silenced and answer with its status.

    The silence is the bash's ``2>/dev/null || true``: the tool's complaints
    are not the caller's to interleave, and a tool that is missing is a state
    the caller already handles.
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


def _capture(argv: list[str]) -> bytes:
    """A tool's stdout, or nothing when it is missing or says nothing."""
    try:
        proc = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except (FileNotFoundError, PermissionError):
        return b""
    return proc.stdout


def _awk_number_printed(value: float) -> str:
    """The number as awk's ``print value`` would render it: an integer value
    with no point, anything else through CONVFMT.

    The page count is awk's ``$2 + 0`` the way the bash prints it, and it is
    the PRINTED form that the ``^[0-9]+$`` test runs against.
    """
    if value == int(value):
        return str(int(value))
    return f"{value:.6g}"


def _page_count(info: bytes) -> str:
    """The bash's first call: ``pdfinfo`` on its own, for the range the per-page
    listings must be asked for.

    The first ``Pages:`` line, its second field as a number; the empty string
    when the line is not there, which is the "page count cannot even be read"
    case.
    """
    for line in info.decode("utf-8", "replace").split("\n"):
        if line.startswith("Pages:"):
            fields = line.split()
            return _awk_number_printed(
                awk_number(fields[1] if len(fields) > 1 else "")
            )
    return ""


def _stats_line(pdf: str, max_height_px: str) -> tuple[str, int]:
    """The three-number line and its status, without printing it: the half of
    ``comic_pdf_stats`` that ``is_comic_pdf`` and ``render_comic_pdf_pages``
    want only the fields of.
    """
    pages = _page_count(_capture(["pdfinfo", pdf]))
    if not _A_NUMBER.fullmatch(pages) or int(pages) < 1:
        return "0 0 0", 1
    pages_n = int(pages)

    # The two listings piped into one awk in the bash, so their bytes go into
    # one stream in the same order, no newline between them added or removed.
    stream = (
        _capture(["pdfinfo", "-f", "1", "-l", str(pages_n), pdf])
        + _capture(["pdfimages", "-list", "-f", "1", "-l", str(pages_n), pdf])
    ).decode("utf-8", "replace")

    min_coverage = _knob("comicPdfMinCoverage", "80")
    min_pixels = _knob("comicPdfMinPixels", "400")
    min_dpi = _knob("comicPdfMinDpi", "150")
    max_dpi = _knob("comicPdfMaxDpi", "600")
    fallback_dpi = _knob("comicPdfFallbackDpi", "300")
    max_height = awk_number(max_height_px)

    page_w: dict[int, float] = {}
    page_h: dict[int, float] = {}
    tallest = 0.0
    images: dict[int, int] = {}
    width: dict[int, float] = {}
    height: dict[int, float] = {}
    xppi: dict[int, float] = {}
    yppi: dict[int, float] = {}
    for line in stream.split("\n"):
        # pdfinfo -f/-l: "Page    3 size: 1275 x 1650 pts". Per page, because a
        # scanned book can change page size halfway through.
        if _PAGE.match(line):
            fields = line.split()
            p = int(awk_number(fields[1]))
            page_w[p] = awk_number(fields[3])
            page_h[p] = awk_number(fields[5])
            if page_h[p] > tallest:
                tallest = page_h[p]
            continue
        # pdfimages -list data row. A "smask" row is the alpha channel of the
        # image above it, not a second image on the page, so only images and
        # stencils are counted.
        fields = line.split()
        if (
            len(fields) >= 12
            and _A_NUMBER.fullmatch(fields[0])
            and _A_NUMBER.fullmatch(fields[1])
            and fields[2] in ("image", "stencil")
        ):
            p = int(awk_number(fields[0]))
            images[p] = images.get(p, 0) + 1
            width[p] = awk_number(fields[3])
            height[p] = awk_number(fields[4])
            # x-ppi/y-ppi are absent from poppler older than 0.25. Then
            # coverage cannot be measured and the pixel floor decides alone.
            xppi[p] = awk_number(fields[12]) if len(fields) >= 14 else 0.0
            yppi[p] = awk_number(fields[13]) if len(fields) >= 14 else 0.0

    good = 0
    native_dpi = 0.0
    for p in range(1, pages_n + 1):
        # Exactly one image. Zero is a text page, more is a layout.
        if images.get(p, 0) != 1:
            continue
        w = width[p]
        h = height[p]
        if w < min_pixels or h < min_pixels:
            continue
        # Coverage, when the placement is known: the size the image is drawn
        # at, in points, against the size of the page - compared as an AREA,
        # so a sideways image counts the same as an upright one.
        if (
            xppi[p] > 0
            and yppi[p] > 0
            and page_w.get(p, 0.0) > 0
            and page_h.get(p, 0.0) > 0
        ):
            drawn = (w / xppi[p] * 72) * (h / yppi[p] * 72)
            if drawn * 100 < page_w[p] * page_h[p] * min_coverage:
                continue
        good += 1
        ppi = xppi[p] if xppi[p] > yppi[p] else yppi[p]
        if ppi > native_dpi:
            native_dpi = ppi

    dpi = native_dpi if native_dpi > 0 else fallback_dpi
    # The cap is worked out on the TALLEST page, so no page renders past the
    # height the caller keeps.
    if max_height > 0 and tallest > 0:
        cap = max_height * 72 / tallest
        if cap < dpi:
            dpi = cap
    if dpi < min_dpi:
        dpi = min_dpi
    if dpi > max_dpi:
        dpi = max_dpi

    return f"{pages_n} {good} {int(dpi + 0.999)}", 0


def comic_pdf_stats(pdf: str, max_height_px: str = "0") -> int:
    """``<pages> <fullPagePages> <dpi>`` - how many pages, how many are a single
    full-page image, and the dpi the pages should be rendered at.

    Prints ``0 0 0`` and returns 1 for a file whose page count cannot even be
    read, so a caller never has to parse an error.
    """
    line, status = _stats_line(pdf, max_height_px)
    print(line)
    return status


def comic_pdf_verdict(pdf: str, max_height_px: str = "0") -> tuple:
    """``(is_a_comic, pages, full_page_pages, dpi)`` - the verdict and the three
    numbers it was reached on, for a caller that wants them rather than a line.

    A comic is a PDF where at least ``comicPdfMinPageShare`` percent of the
    pages are a single full-page image; anything else is a magazine or a text
    document, which rasterising page by page would only turn into a large,
    unreadable .cbz.
    """
    stats, _ = _stats_line(pdf, max_height_px)
    if not stats:
        stats = "0 0 0"
    fields = stats.split()

    def number(index: int) -> int:
        return (int(fields[index])
                if len(fields) > index and _A_NUMBER.fullmatch(fields[index])
                else 0)

    pages, good, dpi = number(0), number(1), number(2)
    if pages <= 0:
        return False, pages, good, dpi
    min_page_share = int(_knob("comicPdfMinPageShare", "90"))
    return good * 100 >= pages * min_page_share, pages, good, dpi


def is_comic_pdf(pdf: str, max_height_px: str = "0") -> int:
    """The verdict as an exit status, the stats line printed either way - so one
    probe answers both "is it?" and "why not?"."""
    comic, pages, good, dpi = comic_pdf_verdict(pdf, max_height_px)
    print("%d %d %d" % (pages, good, dpi))
    return 0 if comic else 1


def comic_pdf_has_pages(directory: str) -> bool:
    """Did the render put anything in ``directory``? -print -quit in the bash,
    so a 400-page book is not walked to answer it.
    """
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith("page") and entry.is_file():
                    return True
    except OSError:
        return False
    return False


def render_comic_pdf_pages(pdf: str, dest: str, max_height_px: str = "0") -> int:
    """One JPEG per page of the comic, in ``dest``, named so they sort in
    reading order.

    Returns 1 when the file yielded no page at all, which is the caller's cue
    to treat the book as it treats an archive that held nothing.
    """
    stats, _ = _stats_line(pdf, max_height_px)
    fields = stats.split()
    dpi = fields[2] if len(fields) > 2 else ""
    if not _A_NUMBER.fullmatch(dpi) or int(dpi) < 1:
        dpi = os.environ.get("comicPdfFallbackDpi", "300")

    try:
        os.makedirs(dest, exist_ok=True)
    except OSError:
        pass

    quality = os.environ.get("comicPdfJpegQuality", "92")
    _run(["pdftoppm", "-jpeg", "-jpegopt", f"quality={quality}",
          "-r", dpi, pdf, f"{dest}/page"])

    # -jpegopt is younger than -jpeg (poppler 0.31). A poppler too old for it
    # rejects the whole invocation, so ask again without it.
    if not comic_pdf_has_pages(dest):
        _run(["pdftoppm", "-jpeg", "-r", dpi, pdf, f"{dest}/page"])

    return 0 if comic_pdf_has_pages(dest) else 1


def comic_pdf_missing_tools() -> int:
    """The poppler tools this library needs and cannot find, space separated,
    or nothing when all three are present.
    """
    missing = [
        tool
        for tool in ("pdfinfo", "pdfimages", "pdftoppm")
        if shutil.which(tool) is None
    ]
    print(" ".join(missing), end="")
    return 0