"""The cover/thumbnail helpers.

Picking the best cover image next to a release, getting it to a sane size, and
embedding it in the finished audio file. The transcode side does the same from
two sources - one already inside the file being transcoded, or a sidecar image
next to it - rather than choosing among the images in a folder.

Every function takes its tuning - the size limit, the dpi, the scratch
directory, the cover thresholds - as arguments and returns its exit status.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence

from medialib.lib import imagemagick, mutagentags
from medialib.lib.enums import shell_lower

__all__ = [
    "choose_thumbnail",
    "extract_thumbnail",
    "embed_thumbnail",
    "apply_cover",
    "extract_source_cover",
]

# The image extensions the ladder knows. Matched on the basename the way
# find -iname would.
_IMAGE_PATTERNS = ("*.jpg", "*.png", "*.webp", "*.avif")

# The refinement passes in INCREASING order of priority, each a group of
# (-iname, negated) conditions that must all hold. A later pass overrides an
# earlier one when it matches anything, so the last non-empty pass wins.
# "cover" and "back" in one name is a BACK cover: the cover pass excludes it,
# which is why the cover+front pass can still reach it.
_LADDER: tuple[tuple[tuple[str, bool], ...], ...] = (
    (("*back*", False),),
    (("*folder*", False),),
    (("*inlay*", False),),
    (("*cover*", False), ("*back*", True)),
    (("*front*", False),),
    (("*cover*", False), ("*front*", False)),
)

Runner = Callable[..., "subprocess.CompletedProcess"]


def _run(argv: Sequence[str], quiet: bool = False) -> subprocess.CompletedProcess:
    """The real runner: the tool on PATH.

    A call the shell version sends to /dev/null does the same here (quiet);
    the rest inherits the streams, the way the unredirected shell calls do.
    """
    if quiet:
        return subprocess.run(list(argv), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    return subprocess.run(list(argv))


def _log(message: str) -> None:
    """log: the one line the module prints, to stderr, the way the shell's log does."""
    if os.environ.get("LOG_TIMESTAMPS"):
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")
    else:
        sys.stderr.write(f"==> {message}\n")


def _iname(name: str, pattern: str) -> bool:
    """find -iname: case-folded wildcard match on the basename.

    The fold is the C library's, the way the shell's is: shell_lower, not
    str.lower - the two differ on U+0130, which lower-cases to a bare "i"
    through the C library and to "i" plus a combining dot in Python.
    """
    return fnmatch.fnmatchcase(shell_lower(name), shell_lower(pattern))


def _is_image_name(name: str) -> bool:
    return any(_iname(name, pattern) for pattern in _IMAGE_PATTERNS)


def _matches_conditions(name: str, conditions: tuple[tuple[str, bool], ...]) -> bool:
    return all(_iname(name, pattern) != negated for pattern, negated in conditions)


def _regular_files(top: str) -> Iterator[str]:
    """``find <top> -type f``: regular files only, in find's own order.

    The order is the filesystem's: each directory's entries in readdir order,
    a subdirectory descended into at once, the way find does it. Sorting would
    change WHICH file a pass picks when several match, and a walk that lists a
    directory's files before its subdirectories would not, either - find
    interleaves them. ``-type f`` is an lstat test, so a link wearing a
    filename is not a file and is not followed.
    """
    def descend(dirpath: str) -> Iterator[str]:
        try:
            entries = list(os.scandir(dirpath))
        except OSError:
            return
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                yield from descend(entry.path)
            elif entry.is_file(follow_symlinks=False):
                yield entry.path

    yield from descend(top)


def _first_file(top: str, predicate) -> str | None:
    for path in _regular_files(top):
        if predicate(os.path.basename(path)):
            return path
    return None


def _remove_quiet(path: str) -> None:
    """rm -rf on a file: gone or never there, and both are a success."""
    try:
        os.remove(path)
    except OSError:
        pass


def _move_over(src: str, dst: str) -> int:
    """``mv -f``: ``src`` takes ``dst``'s place, and a move that lands on
    nothing is the failure the shell's mv left behind - its status, and its one
    line about why.

    shutil.move rather than os.replace, because the source is in the RAM
    scratch and the destination on disk: a rename across filesystems.
    """
    try:
        shutil.move(src, dst)
    except OSError as error:
        _log("    %s" % error)
        return 1
    return 0


def _stem_of(name: str) -> str:
    """${name%.*}: the shortest dot-led suffix stripped, or the whole name."""
    dot = name.rfind(".")
    return name[:dot] if dot >= 0 else name


def _python_bin() -> str:
    """Which interpreter a helper is run with; answered once, in runlog."""
    from medialib.lib import runlog
    return runlog.python_bin()


def choose_thumbnail(input_path: str) -> str:
    """Pick the best cover image (or a PDF page) in <input_path>."""
    first: list[str | None] = [None] * (1 + len(_LADDER))
    for path in _regular_files(input_path):
        name = os.path.basename(path)
        if not _is_image_name(name):
            continue
        if first[0] is None:
            first[0] = path
        for i, conditions in enumerate(_LADDER, start=1):
            if first[i] is None and _matches_conditions(name, conditions):
                first[i] = path
    for i in range(len(first) - 1, -1, -1):
        best = first[i]
        if best is not None:
            return best
    return ""


def extract_thumbnail(input_path: str, file_name: str, dpi: int,
                      have_mkvtoolnix: bool, run: Runner = _run) -> int:
    """Downscale/convert the chosen image for embedding, into <file_name>.jpg.

    From the booklet PDF if there is one (and poppler to read it), else from
    the cover embedded in one of the audio files. Returns the shell function's
    exit status: pdftoppm's own when the PDF was read, 0 otherwise - every
    other branch ends in a call the shell version shields with || true, or
    in an if the shell leaves without an else, and both end at zero.
    """
    pdf = _first_file(input_path,
                      lambda b: _iname(b, "*scan*") and _iname(b, "*.pdf"))
    if pdf is None:
        pdf = _first_file(input_path,
                          lambda b: _iname(b, "*booklet*") and _iname(b, "*.pdf"))

    # A booklet PDF is the PREFERRED cover source, not the only one, so a host
    # without poppler must not lose the cover altogether: forget the PDF and let
    # the embedded-artwork paths have their turn.
    if pdf is not None and shutil.which("pdftoppm") is None:
        _log("    WARNING: pdftoppm not installed, ignoring the booklet PDF "
             "and looking for embedded cover art instead")
        pdf = None

    if pdf is not None:
        return run(["pdftoppm", pdf, file_name, "-jpeg",
                    "-rx", str(dpi), "-ry", str(dpi), "-f", "1",
                    "-singlefile"]).returncode

    source_opus = _first_file(input_path, lambda b: _iname(b, "*.opus"))
    source_mp3 = _first_file(input_path, lambda b: _iname(b, "*.mp3"))
    source_flac = _first_file(input_path, lambda b: _iname(b, "*.flac"))

    if source_opus is not None:
        if have_mkvtoolnix:
            # detour over mka for extractability, kept in RAM
            temp_file = f"{file_name}.extract.mka"
            run(["mkvmerge", "--quiet", "-o", temp_file,
                 "--no-chapters", source_opus])
            run(["mkvextract", "--quiet", temp_file, "attachments",
                 f"1:{file_name}.jpg"])
            _remove_quiet(temp_file)
        else:
            # mkvtoolnix absent: an opus cover stored as a Vorbis-comment PICTURE
            # block is a stream ffmpeg can copy out, the same way the mp3 and
            # flac branches do it.
            run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-i", source_opus, "-an", "-vcodec", "copy",
                 f"{file_name}.jpg"])
        return 0
    if source_mp3 is not None:
        run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", source_mp3, "-an", "-vcodec", "copy",
             f"{file_name}.jpg"])
        return 0
    if source_flac is not None:
        run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", source_flac, "-an", "-vcodec", "copy",
             f"{file_name}.jpg"])
        return 0
    return 0


def embed_thumbnail(input_path: str, file_name: str, image_size_limit: int,
                    jpg_quality_level: int, thumbnail_resolution: str,
                    dpi: int, have_mkvtoolnix: bool, ram_dir: str,
                    script_dir: str, run: Runner = _run) -> int:
    """Write the chosen thumbnail into the output file(s) next to <file_name>."""
    file_name = file_name.removesuffix("/")
    opus_file = f"{file_name}.opus"
    mp3_file = f"{file_name}.mp3"
    m4b_file = f"{file_name}.m4b"
    flac_file = f"{file_name}.flac"
    ram_base = f"{ram_dir}/{os.path.basename(file_name)}"
    m4b_temp_file = f"{ram_base}.temp.m4b"

    thumb_file = choose_thumbnail(input_path)

    # or extract from pdf file or audio file themselves, if nothing was found till now
    if not os.path.isfile(thumb_file):
        extract_thumbnail(input_path, ram_base, dpi, have_mkvtoolnix, run)
        thumb_file = f"{ram_base}.jpg"

    # The temp name is keyed on ramBase (the unique per-subfolder output stem),
    # NOT on the source image's basename: parallel workers share one ram_dir.
    output_thumb = f"{ram_base}.output.jpg"

    if os.path.isfile(thumb_file):
        file_size = os.stat(thumb_file).st_size
        if file_size >= image_size_limit or thumb_file[-4:] != ".jpg":
            run(imagemagick.convert_argv(
                ["-quiet", thumb_file,
                 "-quality", str(jpg_quality_level),
                 "-resize", f"{thumbnail_resolution}>", output_thumb]))
        else:
            shutil.copyfile(thumb_file, output_thumb)

    # The if the shell leaves without an else ends at zero, whatever branch
    # it took or skipped - only a move that lands on nothing fails it.
    if os.path.isfile(output_thumb):
        if os.path.isfile(opus_file):
            mutagentags.embed_cover(opus_file, output_thumb)
            _remove_quiet(output_thumb)
            return 0
        if os.path.isfile(flac_file):
            mutagentags.embed_cover(flac_file, output_thumb)
            _remove_quiet(output_thumb)
            return 0
        if os.path.isfile(mp3_file):
            mp3_temp_file = f"{ram_base}.temp.mp3"
            run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-i", mp3_file, "-i", output_thumb,
                 "-c", "copy", "-map", "0", "-map", "1", mp3_temp_file])
            _remove_quiet(mp3_file)
            _remove_quiet(output_thumb)
            return _move_over(mp3_temp_file, mp3_file)
        if os.path.isfile(m4b_file):
            run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-i", m4b_file, "-i", output_thumb,
                 "-c", "copy", "-disposition:v:0", "attached_pic",
                 m4b_temp_file])
            _remove_quiet(m4b_file)
            _remove_quiet(output_thumb)
            return _move_over(m4b_temp_file, m4b_file)
    return 0


def apply_cover(track: str, target: str | None, cover_threshold: int,
                cover_quality: int, cover_resolution: str, input_dir: str,
                output_dir: str, tmp_dir: str, opus: str, script_dir: str,
                run: Runner = _run) -> int:
    """Let a sidecar cover override the source's, then embed it in <target>.

    <target> defaults to <opus>, the way the shell's ${2:-$opus} does.
    """
    if not target:
        target = opus
    temp_cover = f"{tmp_dir}/tempCover.jpg"
    cover = f"{tmp_dir}/cover.jpg"
    stem = _stem_of(track)
    disk_cover = f"{output_dir}/{stem}.jpg"
    disk_cover_webp = f"{output_dir}/{stem}.webp"

    # The sidecar is an image sharing the track's base name in the INPUT
    # folder, where it always sits next to the source audio. Copied (not
    # moved) so the source image is preserved.
    if os.path.isfile(f"{input_dir}/{stem}.webp"):
        shutil.copyfile(f"{input_dir}/{stem}.webp", temp_cover)
    elif os.path.isfile(f"{input_dir}/{stem}.jpg"):
        shutil.copyfile(f"{input_dir}/{stem}.jpg", temp_cover)

    if not os.path.isfile(temp_cover):
        return 0

    file_size = os.stat(temp_cover).st_size
    if file_size >= cover_threshold:
        run(imagemagick.convert_argv(
            [temp_cover,
             "-quality", str(cover_quality),
             "-resize", f"{cover_resolution}>", cover]), quiet=True)
    else:
        shutil.copyfile(temp_cover, cover)

    # The last command the shell function leaves is an if without an else,
    # so whatever the embed did the status is zero; only a successful embed
    # drops the now-redundant sidecar copies in the output folder.
    if mutagentags.embed_cover(target, cover) == 0:
        _remove_quiet(disk_cover)
        _remove_quiet(disk_cover_webp)
    return 0


def extract_source_cover(src: str, tmp_dir: str, run: Runner = _run) -> int:
    """Pull the source file's embedded cover into <tmp_dir>/tempCover.jpg.

    A Matroska attachment via mkvextract for mka/mkv, falling back to the
    embedded cover stream via ffmpeg; the stream straight out otherwise.
    """
    temp_cover = f"{tmp_dir}/tempCover.jpg"
    lowered = shell_lower(src)
    if lowered.endswith(".mka") or lowered.endswith(".mkv"):
        rc = run(["mkvextract", src, "attachments", f"1:{temp_cover}"],
                 quiet=True).returncode
        if rc != 0:
            run(["ffmpeg", "-nostdin", "-i", src, "-an", "-c:v", "copy",
                 temp_cover], quiet=True)
    else:
        run(["ffmpeg", "-nostdin", "-i", src, "-an", "-c:v", "copy",
             temp_cover], quiet=True)
    return 0