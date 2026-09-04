"""The tags ffmpeg cannot write: cover art and chapters in a Vorbis comment.

ffmpeg has no way to write a FLAC picture block or a METADATA_BLOCK_PICTURE
comment, and neither FLAC nor Ogg/Opus has a Matroska-style chapter container, so
chapters go in as the de-facto OGM convention::

    CHAPTER01=00:00:00.000
    CHAPTER01NAME=Intro

Both containers carry Vorbis comments, so the handling is identical either way and
only the loader differs by extension.

**Every function here answers with a process-style status: 0 for done, non-zero
for failed**, because that is the error boundary its callers need: a corrupt file
is one file `ingest_music` counts as `chaptersFailed` and reports, where a raise
would take the whole run down.

``mutagen`` is imported inside the functions rather than at the top, so a host
without it can still import this package and run everything that does not write a
tag - which is also where the preflight (``tooldeps.require_python_module``) puts
its error message.
"""

from __future__ import annotations

import base64
import os
import sys

__all__ = ["embed_chapters", "embed_cover", "remove_cover"]

# Named here because the refusal below tells the reader how to override it.
FORCE_FLAG = "--force"


def _picture(cover: str):
    from mutagen.flac import Picture

    picture = Picture()
    picture.mime = "image/jpeg"
    with open(cover, "rb") as handle:
        picture.data = handle.read()
    picture.type = 3  # front cover
    return picture


def embed_cover(audio: str, cover: str) -> int:
    """Put <cover> in <audio>, replacing whatever cover is there.

    FLAC keeps a picture in a native PICTURE block, and the existing ones are
    cleared first so a rerun replaces rather than accumulates. Opus carries the
    same structure base64-encoded in a METADATA_BLOCK_PICTURE comment.
    """
    try:
        picture = _picture(cover)
        if audio.lower().endswith(".flac"):
            from mutagen.flac import FLAC

            handle = FLAC(audio)
            handle.clear_pictures()
            handle.add_picture(picture)
            handle.save()
            return 0

        from mutagen.oggopus import OggOpus

        opus = OggOpus(audio)
        # Through the file rather than through `.tags`, which mutagen types as
        # optional - the same mapping the line below and `embed_chapters` already
        # use, and it answers "no picture" for a file with no tags at all rather
        # than raising on the membership test.
        if "METADATA_BLOCK_PICTURE" in opus:
            del opus["METADATA_BLOCK_PICTURE"]
        opus["METADATA_BLOCK_PICTURE"] = base64.b64encode(
            picture.write()).decode("ascii")
        opus.save()
        return 0
    except Exception:
        return 1


def remove_cover(opus: str) -> int:
    """Take the cover back off an Opus file. Nothing in the pipelines calls this;
    it is here for the times a wrong cover has to be removed by hand."""
    try:
        from mutagen.oggopus import OggOpus

        handle = OggOpus(opus)
        if "METADATA_BLOCK_PICTURE" in handle:
            del handle["METADATA_BLOCK_PICTURE"]
        handle.save()
        return 0
    except Exception:
        return 1


def _open(audio: str):
    if audio.lower().endswith(".opus"):
        from mutagen.oggopus import OggOpus

        return OggOpus(audio)
    from mutagen.flac import FLAC

    return FLAC(audio)


def _chapter_keys(handle) -> list[str]:
    # A snapshot, not a view: the caller deletes through it.
    keys = list(handle.keys())
    return [key for key in keys if key.upper().startswith("CHAPTER")]


def _chapter_fields(chapter_file: str) -> list[tuple[str, str]]:
    fields = []
    with open(chapter_file, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\n").rstrip("\r")
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                fields.append((key, value))
    return fields


def embed_chapters(audio: str, chapter_file: str, title: str = "",
                   force: bool = False, error=None) -> int:
    """Write <chapter_file>'s CHAPTER lines into <audio>, and the title with them.

    Chapters already in the file are KEPT, so marks placed by hand survive a
    rerun, and ``force`` makes the chapter file the whole truth instead. The
    title is written either way.

    <chapter_file> may be empty, or ``/dev/null``, when there are no chapters:
    only the title is then written.

    **Keeping is not a failure.** The status is 0 whether the chapters were
    written or the existing ones won - which is what the callers have always
    seen, because the script this replaces discarded that distinction on its way
    to `exit 0`. Only an error is non-zero.
    """
    error = error if error is not None else sys.stderr
    try:
        handle = _open(audio)
        existing = _chapter_keys(handle)
        if existing and not force:
            kept = len([k for k in existing if not k.upper().endswith("NAME")])
            error.write("kept the %d chapter(s) already in %s (pass %s to "
                        "replace them)\n" % (kept, audio, FORCE_FLAG))
            if title:
                handle["TITLE"] = title
                handle.save()
            return 0

        for key in existing:
            del handle[key]
        if chapter_file and os.path.exists(chapter_file):
            for key, value in _chapter_fields(chapter_file):
                # Vorbis-comment names are conventionally upper case, and the
                # CHAPTER convention relies on the exact CHAPTERnn /
                # CHAPTERnnNAME spelling the chapter file already carries.
                handle[key] = value
        if title:
            handle["TITLE"] = title
        handle.save()
        return 0
    except Exception:
        return 1
