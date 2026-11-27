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
from collections.abc import Callable, Iterator, Sequence

from medialib.lib import (
    aicover,
    formatting,
    imagecodecs,
    imagemagick,
    mutagentags,
    onlinecover,
    runlog,
    toolcapture,
    treewalk,
)
from medialib.lib.enums import (
    AUDIO_EXTENSIONS,
    LOSSLESS_AUDIO_EXTENSIONS,
    lower_extension_of,
    shell_lower,
)
from medialib.lib.versionsort import version_key

_AUDIO_EXTENSIONS = frozenset((*AUDIO_EXTENSIONS, *LOSSLESS_AUDIO_EXTENSIONS))

__all__ = [
    "choose_thumbnail",
    "extract_thumbnail",
    "embed_thumbnail",
    "apply_cover",
    "embed_cover_file",
    "extract_source_cover",
    "online_book_cover",
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

Runner = Callable[..., "toolcapture.ToolRun"]


def _run(argv: Sequence[str], may_fail: bool = False) -> toolcapture.ToolRun:
    """The real runner: the tool on PATH, its output kept rather than shown.

    A failure is a warning with the tool's own words under it: a cover is
    metadata, and a step that did not complete leaves the book as it was.
    ``may_fail`` is for a call whose failure is an answer rather than a
    problem - asking a file for a cover it does not have - and says nothing.
    """
    return toolcapture.run(
        list(argv), report=not may_fail,
        warning="cover not written, so the book keeps what it had")


_log = runlog.log
_detail = runlog.detail


def _iname(name: str, pattern: str) -> bool:
    """find -iname: case-folded wildcard match on the basename.

    The fold is the C library's: shell_lower, not str.lower - the two differ
    on U+0130, which lower-cases to a bare "i" through the C library and to
    "i" plus a combining dot in Python.
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
    for entry in treewalk.entries_below(top):
        if entry.is_file(follow_symlinks=False):
            yield entry.path


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
    """``mv -f``: ``src`` takes ``dst``'s place; a move that lands on nothing
    fails, answered with its status and its one line about why.

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


def choose_thumbnail(input_path: str, *, model_meter: str = "",
                     scratch_dir: str = "",
                     lookers: Sequence[aicover.llmclient.Llm] | None = None,
                     prepare: Callable[[str, str], str] | None = None) -> str:
    """Pick the best cover image (or a PDF page) in <input_path>.

    The name ladder (:data:`_LADDER`) decides first. Only when no name rung
    above the back cover's matches and the folder holds two or more images is
    a vision model (:func:`aicover.choose_folder_cover`) asked which one is
    the front cover; on "none", error, switch off, or no vision model, the
    back cover is kept where there is one, else the first image the folder
    walk lists. A back cover is the ladder's last resort, not a front: a
    folder of scans named Back, Label and Page 01 has its front among the
    others."""
    first: list[str | None] = [None] * len(_LADDER)
    images: list[str] = []
    for path in _regular_files(input_path):
        name = os.path.basename(path)
        if not _is_image_name(name):
            continue
        images.append(path)
        for i, conditions in enumerate(_LADDER):
            if first[i] is None and _matches_conditions(name, conditions):
                first[i] = path
    for i in range(len(first) - 1, 0, -1):
        best = first[i]
        if best is not None:
            return best
    if not images:
        return ""
    if len(images) >= 2:
        picked = aicover.choose_folder_cover(
            input_path, images, lookers=lookers, meter_file=model_meter,
            scratch_dir=scratch_dir, prepare=prepare)
        if picked:
            return picked
    return first[0] or images[0]


def extract_thumbnail(input_path: str, file_name: str, dpi: int,
                      have_mkvtoolnix: bool, run: Runner = _run,
                      pdf_dir: str = "") -> int:
    """Downscale/convert the chosen image for embedding, into <file_name>.jpg.

    From the booklet PDF if there is one (and poppler to read it), else from
    the cover embedded in one of the audio files. Returns pdftoppm's own exit
    status when the PDF was read, 0 otherwise - every other branch ends at
    zero. The PDF is looked for in ``pdf_dir`` where given - the folder the
    audio was transcoded from - and in ``input_path`` otherwise.
    """
    pdf_dir = pdf_dir or input_path
    pdf = _first_file(pdf_dir,
                      lambda b: _iname(b, "*scan*") and _iname(b, "*.pdf"))
    if pdf is None:
        pdf = _first_file(pdf_dir,
                          lambda b: _iname(b, "*booklet*") and _iname(b, "*.pdf"))

    # A booklet PDF is the PREFERRED cover source, not the only one, so a host
    # without poppler must not lose the cover altogether: forget the PDF and let
    # the embedded-artwork paths have their turn.
    if pdf is not None and shutil.which("pdftoppm") is None:
        _log("    WARNING: pdftoppm not installed, ignoring the booklet PDF "
             "and looking for embedded cover art instead")
        pdf = None

    if pdf is not None:
        status = run(["pdftoppm", pdf, file_name, "-jpeg",
                      "-rx", str(dpi), "-ry", str(dpi), "-f", "1",
                      "-singlefile"]).returncode
        # Said only when the page came out: a pdftoppm that wrote nothing
        # leaves the embedded-artwork rungs their turn, and they say theirs.
        if os.path.isfile(f"{file_name}.jpg"):
            _detail("    Cover from the first page of the PDF booklet %s"
                    % os.path.relpath(pdf, pdf_dir))
        return status

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
                 f"1:{file_name}.jpg"], may_fail=True)
            _remove_quiet(temp_file)
        else:
            # mkvtoolnix absent: an opus cover stored as a Vorbis-comment PICTURE
            # block is a stream ffmpeg can copy out, the same way the mp3 and
            # flac branches do it.
            run(["ffmpeg", "-nostdin", "-hide_banner",
                 *toolcapture.ffmpeg_loglevel(),
                 "-i", source_opus, "-an", "-vcodec", "copy",
                 f"{file_name}.jpg"], may_fail=True)
        _say_embedded(source_opus, file_name, input_path)
        return 0
    if source_mp3 is not None:
        run(["ffmpeg", "-nostdin", "-hide_banner",
             *toolcapture.ffmpeg_loglevel(),
             "-i", source_mp3, "-an", "-vcodec", "copy",
             f"{file_name}.jpg"], may_fail=True)
        _say_embedded(source_mp3, file_name, input_path)
        return 0
    if source_flac is not None:
        run(["ffmpeg", "-nostdin", "-hide_banner",
             *toolcapture.ffmpeg_loglevel(),
             "-i", source_flac, "-an", "-vcodec", "copy",
             f"{file_name}.jpg"], may_fail=True)
        _say_embedded(source_flac, file_name, input_path)
        return 0
    return 0


def _say_embedded(source: str, file_name: str, input_path: str) -> None:
    """The line for a cover that came out of one of the audio files, said
    only when the extraction actually left a picture behind: a file that
    carries none prints no line, and the rung below says its own."""
    if os.path.isfile(f"{file_name}.jpg"):
        _detail("    Cover from the art embedded in %s"
                % os.path.relpath(source, input_path))


def _online_thumbnail(input_path: str, outputs: Sequence[str],
                      ram_base: str, model_meter: str = "",
                      cover_meta: dict[str, str] | None = None) -> str:
    """A cover found online for the book joined from <input_path>, or "".

    The book's length and channel count are read off the joined output: they
    say what the book probably is and which answer is it. Podcast length in
    stereo is most likely a full-cast drama, since convert-audio keeps a talk
    mono.

    A cover found is kept in <input_path> as well (:func:`_keep_in_input`),
    so the folder has its cover from then on.
    """
    joined = next((path for path in outputs if os.path.isfile(path)), "")
    seconds = onlinecover.media_seconds(joined) if joined else 0.0
    stereo = bool(joined) and onlinecover.media_channels(joined) >= 2
    image = online_book_cover(input_path, seconds, stereo, ram_base,
                              model_meter, cover_meta)
    if image:
        _keep_in_input(image, input_path)
    return image


def online_book_cover(input_path: str, seconds: float, stereo: bool,
                      ram_base: str, model_meter: str = "",
                      cover_meta: dict[str, str] | None = None,
                      root: str = "") -> str:
    """A cover found online for the book in <input_path>, saved beside
    <ram_base>, or "". The catalogues first (:mod:`onlinecover`), then a
    model that can search the web (:mod:`aicover`, only under -A), told
    everything the catalogues were. The tags are read off the folder's audio
    tracks; <input_path> itself is only read. <root> is the folder the run
    was given, whose folders down to <input_path> are clues too."""
    tracks = [path for path in _regular_files(input_path)
              if lower_extension_of(path) in _AUDIO_EXTENSIONS]
    ordered = sorted(tracks, key=version_key)
    subject = onlinecover.book_subject(input_path, ordered, seconds, stereo,
                                       meter_file=model_meter, root=root)
    image = onlinecover.book_cover(input_path, ordered, seconds,
                                   f"{ram_base}.online", stereo=stereo,
                                   subject=subject)
    if not image:
        image = aicover.book_cover(
            input_path, os.path.basename(ram_base),
            ordered, seconds,
            f"{ram_base}.model", stereo=stereo, subject=subject,
            meter_file=model_meter, found_meta=cover_meta)
    return image


# The name a cover found online is kept under in the input folder: one the
# ladder's cover pass picks first on the next run.
KEPT_COVER_STEM = "cover"


def _keep_in_input(image: str, input_path: str) -> str:
    """A copy of ``image`` in ``input_path`` as cover.<ext>, or under a
    numbered name where that is taken - never over a file. Returns the copy,
    or "" when the folder would not take it, which is said and is no
    failure: the book still gets its cover."""
    extension = os.path.splitext(image)[1] or ".jpg"
    for number in range(1, 100):
        name = KEPT_COVER_STEM + ("" if number == 1 else " (%d)" % number) \
            + extension
        target = os.path.join(input_path, name)
        created = False
        try:
            with open(target, "xb") as out:
                created = True
                with open(image, "rb") as source:
                    shutil.copyfileobj(source, out)
        except FileExistsError:
            continue
        except OSError as error:
            if created:
                _remove_quiet(target)
            _log("    WARNING: the cover found online was not kept in the "
                 "input folder: %s" % error)
            return ""
        _detail("    Kept the cover found online in the input as %s" % name)
        return target
    return ""


def embed_thumbnail(input_path: str, file_name: str, image_size_limit: int,
                    jpg_quality_level: int, thumbnail_resolution: str,
                    dpi: int, have_mkvtoolnix: bool, ram_dir: str,
                    script_dir: str, run: Runner = _run,
                    model_meter: str = "",
                    cover_meta: dict[str, str] | None = None) -> int:
    """Write the chosen thumbnail into the output file(s) next to <file_name>.

    <model_meter> is where what a model was asked for the cover is recorded
    for the run's total (:func:`modelmeter.record`). <cover_meta>, when given,
    receives any metadata the cover model returned alongside the chosen cover."""
    file_name = file_name.removesuffix("/")
    opus_file = f"{file_name}.opus"
    mp3_file = f"{file_name}.mp3"
    m4b_file = f"{file_name}.m4b"
    flac_file = f"{file_name}.flac"
    ram_base = f"{ram_dir}/{os.path.basename(file_name)}"
    m4b_temp_file = f"{ram_base}.temp.m4b"

    # Under convert-and-concat the folder is its intermediate tree, which
    # holds the transcoded audio alone: the images and the booklet beside the
    # source stay there, and are looked for there.
    cover_dir = onlinecover.source_folder(input_path)
    thumb_file = choose_thumbnail(cover_dir, model_meter=model_meter,
                                  scratch_dir=ram_dir)
    if os.path.isfile(thumb_file):
        _detail("    Cover from the image file %s in the input"
                % os.path.relpath(thumb_file, cover_dir))

    if not os.path.isfile(thumb_file):
        extract_thumbnail(input_path, ram_base, dpi, have_mkvtoolnix, run,
                          pdf_dir=cover_dir)
        thumb_file = f"{ram_base}.jpg"

    # Online is the last rung, when none of the local ones held a cover.
    if not os.path.isfile(thumb_file):
        if onlinecover.enabled():
            thumb_file = _online_thumbnail(
                input_path, (opus_file, mp3_file, m4b_file, flac_file),
                ram_base, model_meter, cover_meta)
        else:
            _detail("    No cover looked for online: the coverSearch "
                    "setting is off")

    # The temp name is keyed on ram_base (the unique per-subfolder output stem),
    # NOT on the source image's basename: parallel workers share one ram_dir.
    output_thumb = f"{ram_base}.output.jpg"

    if os.path.isfile(thumb_file):
        file_size = os.stat(thumb_file).st_size
        if file_size >= image_size_limit or thumb_file[-4:] != ".jpg":
            run(imagemagick.convert_argv(
                ["-quiet", imagecodecs.coder_prefixed(thumb_file),
                 "-quality", str(jpg_quality_level),
                 "-resize", f"{thumbnail_resolution}>", output_thumb]))
        else:
            shutil.copyfile(thumb_file, output_thumb)

    # The block ends at zero, whatever branch it took or skipped - only a
    # move that lands on nothing fails it.
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
            run(["ffmpeg", "-nostdin", "-hide_banner",
                 *toolcapture.ffmpeg_loglevel(),
                 "-i", mp3_file, "-i", output_thumb,
                 "-c", "copy", "-map", "0", "-map", "1", mp3_temp_file])
            _remove_quiet(mp3_file)
            _remove_quiet(output_thumb)
            return _move_over(mp3_temp_file, mp3_file)
        if os.path.isfile(m4b_file):
            run(["ffmpeg", "-nostdin", "-hide_banner",
                 *toolcapture.ffmpeg_loglevel(),
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
                run: Runner = _run,
                origin: str = "the source's own art",
                out_track: str | None = None) -> int:
    """Let a sidecar cover override the source's, then embed it in <target>.

    <target> defaults to <opus>. <origin> is where a cover that is not a
    sidecar was taken from - the source's own art, or an online search.
    <out_track> is <track> as it is named under <output_dir>, where the
    sidecar's copy is dropped once embedded; <track> itself unless given.
    """
    if not target:
        target = opus
    temp_cover = f"{tmp_dir}/tempCover.jpg"
    cover = f"{tmp_dir}/cover.jpg"
    stem = _stem_of(track)
    out_stem = _stem_of(out_track) if out_track else stem
    disk_cover = f"{output_dir}/{out_stem}.jpg"
    disk_cover_webp = f"{output_dir}/{out_stem}.webp"

    # The sidecar is an image sharing the track's base name in the INPUT
    # folder, where it always sits next to the source audio. Copied (not
    # moved) so the source image is preserved. It overrides <origin>, the
    # source a non-sidecar cover was taken from.
    if os.path.isfile(f"{input_dir}/{stem}.webp"):
        shutil.copyfile(f"{input_dir}/{stem}.webp", temp_cover)
        origin = "a sidecar image"
    elif os.path.isfile(f"{input_dir}/{stem}.jpg"):
        shutil.copyfile(f"{input_dir}/{stem}.jpg", temp_cover)
        origin = "a sidecar image"

    if not os.path.isfile(temp_cover):
        return 0

    # Whatever the embed did, the status is zero; only a successful embed
    # drops the now-redundant sidecar copies in the output folder.
    if embed_cover_file(track, target, temp_cover, cover, cover_threshold,
                        cover_quality, cover_resolution, run,
                        origin=origin) == 0:
        _remove_quiet(disk_cover)
        _remove_quiet(disk_cover_webp)
    return 0


def embed_cover_file(track: str, target: str, image: str, cover: str,
                     cover_threshold: int, cover_quality: int,
                     cover_resolution: str, run: Runner = _run,
                     origin: str = "the source's own art") -> int:
    """<image> embedded in <target> by way of <cover>: recompressed to
    <cover_resolution> at <cover_quality> when it is <cover_threshold> bytes
    or more, copied as it is otherwise. The embed's status - 0 when <target>
    carries it. <track> and <origin> are what -v says it was, and from where.
    """
    file_size = os.stat(image).st_size
    if file_size >= cover_threshold:
        run(imagemagick.convert_argv(
            [imagecodecs.coder_prefixed(image),
             "-quality", str(cover_quality),
             "-resize", f"{cover_resolution}>", cover]))
        recompressed = True
    else:
        shutil.copyfile(image, cover)
        recompressed = False
    _detail("    %s: cover from %s, %s, %s"
            % (track, origin, formatting.fmt_bytes(file_size),
               "recompressed" if recompressed else "kept at its own size"))
    return mutagentags.embed_cover(target, cover)


def extract_source_cover(src: str, tmp_dir: str, run: Runner = _run) -> int:
    """Pull the source file's embedded cover into <tmp_dir>/tempCover.jpg.

    A Matroska attachment via mkvextract for mka/mkv, falling back to the
    embedded cover stream via ffmpeg; the stream straight out otherwise.
    """
    temp_cover = f"{tmp_dir}/tempCover.jpg"
    lowered = shell_lower(src)
    if lowered.endswith(".mka") or lowered.endswith(".mkv"):
        rc = run(["mkvextract", src, "attachments", f"1:{temp_cover}"],
                 may_fail=True).returncode
        if rc != 0:
            run(["ffmpeg", "-nostdin", "-i", src, "-an", "-c:v", "copy",
                 temp_cover], may_fail=True)
    else:
        run(["ffmpeg", "-nostdin", "-i", src, "-an", "-c:v", "copy",
             temp_cover], may_fail=True)
    return 0