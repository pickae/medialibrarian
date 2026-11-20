"""The tags ffmpeg cannot write: cover art and chapters in a Vorbis comment.

ffmpeg has no way to write a FLAC picture block or a METADATA_BLOCK_PICTURE
comment, and neither FLAC nor Ogg/Opus has a Matroska-style chapter container, so
chapters go in as the de-facto OGM convention::

    CHAPTER01=00:00:00.000
    CHAPTER01NAME=Intro

Both containers carry Vorbis comments, so the handling is identical either way and
only the loader differs by extension.

MP4 is the third container, and it is only half here. Its cover art is a native
``covr`` atom, which mutagen writes as readily as the other two - but MP4 has no
Vorbis comments and no place to put a CHAPTERnn row, so chapters into an .m4a are
not a tag at all: they are a chapter TRACK, and ffmpeg writes those. That half
lives in :mod:`medialib.lib.chapters`, beside the m4b path that writes one the
same way.

**Every function here answers with a process-style status: 0 for done, non-zero
for failed**, because a corrupt file is one file failed and reported, where a
raise would take the whole run down.

``mutagen`` is imported inside the functions rather than at the top, so a host
without it can still import this package and run everything that does not write a
tag - which is also where the preflight (``tooldeps.require_python_module``) puts
its error message.
"""

from __future__ import annotations

import base64
import os
import sys
from collections.abc import Mapping

__all__ = ["BOOK_TAG_FIELDS", "MP4_SUFFIXES", "embed_chapters", "embed_cover",
           "fill_book_tags", "has_tag", "read_book_tags", "set_tag"]

# The containers whose metadata is MP4's rather than a Vorbis comment's: a cover
# is a `covr` atom, and chapters are a track this module cannot write at all.
# One definition, because both halves of that split have to agree about which
# files they are talking about - :mod:`medialib.lib.chapters` reads it for the
# other half.
MP4_SUFFIXES = (".m4a", ".m4b", ".mp4")

_VORBIS_SUFFIXES = (".opus", ".flac", ".ogg", ".oga")

# The canonical audiobook tag fields :func:`read_book_tags` and
# :func:`fill_book_tags` know how to read and write across Vorbis, MP4 and MP3.
BOOK_TAG_FIELDS = ("title", "author", "narrator", "series", "series_index",
                   "year", "language", "abridged")

# Vorbis comment mapping: canonical field -> (write key, existing-check keys).
_VORBIS_BOOK_TAGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "title": ("TITLE", ("TITLE",)),
    "author": ("ARTIST", ("ARTIST", "AUTHOR")),
    "narrator": ("NARRATOR", ("NARRATOR",)),
    "series": ("SERIES", ("SERIES", "MOVEMENTNAME")),
    "series_index": ("SERIES-PART", ("SERIES-PART", "SERIES_INDEX", "MOVEMENT")),
    "year": ("DATE", ("DATE", "YEAR")),
    "language": ("LANGUAGE", ("LANGUAGE",)),
    "abridged": ("ABRIDGED", ("ABRIDGED",)),
}

# MP4 atom mapping: canonical field -> (write key, is_freeform, existing-check keys).
_MP4_BOOK_TAGS: dict[str, tuple[str, bool, tuple[str, ...]]] = {
    "title": ("\xa9nam", False, ("\xa9nam",)),
    "author": ("\xa9ART", False, ("\xa9ART",)),
    "narrator": ("\xa9nrt", False, ("\xa9nrt", "----:com.apple.iTunes:NARRATOR")),
    "series": ("----:com.apple.iTunes:SERIES", True,
               ("----:com.apple.iTunes:SERIES", "\xa9mvn")),
    "series_index": ("----:com.apple.iTunes:SERIES-PART", True,
                     ("----:com.apple.iTunes:SERIES-PART",
                      "----:com.apple.iTunes:SERIES_INDEX")),
    "year": ("\xa9day", False, ("\xa9day",)),
    "language": ("----:com.apple.iTunes:LANGUAGE", True,
                 ("----:com.apple.iTunes:LANGUAGE",)),
    "abridged": ("----:com.apple.iTunes:ABRIDGED", True,
                 ("----:com.apple.iTunes:ABRIDGED",)),
}

# ID3 mapping: canonical field -> (frame_id_or_txxx_desc, is_txxx, frame_checks, txxx_descs).
_MP3_BOOK_TAGS: dict[str, tuple[str, bool, tuple[str, ...], tuple[str, ...]]] = {
    "title": ("TIT2", False, ("TIT2",), ()),
    "author": ("TPE1", False, ("TPE1",), ()),
    "narrator": ("NARRATOR", True, (), ("narrator",)),
    "series": ("SERIES", True, ("MVNM",), ("series",)),
    "series_index": ("SERIES-PART", True, ("MVIN",), ("series-part", "series_index")),
    "year": ("TDRC", False, ("TDRC", "TYER"), ()),
    "language": ("TLAN", False, ("TLAN",), ()),
    "abridged": ("ABRIDGED", True, (), ("abridged",)),
}

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
    same structure base64-encoded in a METADATA_BLOCK_PICTURE comment. MP4 keeps
    neither: a `covr` atom holds the JPEG bytes on their own, with no picture
    structure around them, so it is the one branch that does not build one.
    """
    try:
        if audio.lower().endswith(MP4_SUFFIXES):
            from mutagen.mp4 import MP4, MP4Cover

            mp4 = MP4(audio)
            with open(cover, "rb") as image:
                data = image.read()
            # Assigned rather than appended, so a rerun replaces the cover the
            # way the other two branches do instead of stacking a second one.
            mp4["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG)]
            mp4.save()
            return 0

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


def _open(audio: str):
    if audio.lower().endswith(".opus"):
        from mutagen.oggopus import OggOpus

        return OggOpus(audio)
    if audio.lower().endswith((".ogg", ".oga")):
        # An .ogg may hold Vorbis, Opus or FLAC: mutagen reads which.
        import mutagen

        handle = mutagen.File(audio)
        if handle is None:
            raise ValueError("not an Ogg file mutagen reads: %s" % audio)
        return handle
    from mutagen.flac import FLAC

    return FLAC(audio)


def has_tag(audio: str, name: str) -> bool:
    """Whether an Opus or FLAC file holds this Vorbis comment, non-empty."""
    try:
        return any(value.strip() for value in _open(audio).get(name) or [])
    except Exception:
        return False


def set_tag(audio: str, name: str, value: str) -> int:
    """One Vorbis comment of an Opus or FLAC file set to ``value``, replacing
    whatever it held."""
    try:
        handle = _open(audio)
        handle[name] = value
        handle.save()
        return 0
    except Exception:
        return 1


def _vorbis_read_field(handle, check_keys: tuple[str, ...]) -> str:
    for key in check_keys:
        for entry in handle.get(key) or ():
            text = str(entry).strip()
            if text:
                return text
    return ""


def _mp4_text(values) -> str:
    for item in values or ():
        text = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
        text = text.strip()
        if text:
            return text
    return ""


def _mp4_read_field(handle, check_keys: tuple[str, ...]) -> str:
    lower_map = {str(k).lower(): k for k in handle}
    for key in check_keys:
        found_key = key if key in handle else lower_map.get(key.lower())
        if found_key is not None:
            text = _mp4_text(handle.get(found_key))
            if text:
                return text
    return ""


def _mp3_read_field(tags, frame_checks: tuple[str, ...],
                    txxx_descs: tuple[str, ...]) -> str:
    if tags is None:
        return ""
    for frame_id in frame_checks:
        for frame in tags.getall(frame_id):
            for entry in getattr(frame, "text", ()) or ():
                text = str(entry).strip()
                if text:
                    return text
    if txxx_descs:
        for frame in tags.getall("TXXX"):
            if str(getattr(frame, "desc", "")).strip().lower() in txxx_descs:
                for entry in getattr(frame, "text", ()) or ():
                    text = str(entry).strip()
                    if text:
                        return text
    return ""


def read_book_tags(audio: str) -> dict[str, str]:
    """The non-empty canonical audiobook tags already carried by ``audio``
    (see :data:`BOOK_TAG_FIELDS`). Empty dict for an unreadable or unsupported
    file."""
    lower = audio.lower()
    try:
        if lower.endswith(_VORBIS_SUFFIXES):
            handle = _open(audio)
            return {field: value for field in BOOK_TAG_FIELDS
                    if (value := _vorbis_read_field(
                        handle, _VORBIS_BOOK_TAGS[field][1]))}
        if lower.endswith(MP4_SUFFIXES):
            from mutagen.mp4 import MP4

            mp4 = MP4(audio)
            return {field: value for field in BOOK_TAG_FIELDS
                    if (value := _mp4_read_field(
                        mp4, _MP4_BOOK_TAGS[field][2]))}
        if lower.endswith(".mp3"):
            from mutagen.mp3 import MP3

            mp3 = MP3(audio)
            return {field: value for field in BOOK_TAG_FIELDS
                    if (value := _mp3_read_field(
                        mp3.tags, _MP3_BOOK_TAGS[field][2],
                        _MP3_BOOK_TAGS[field][3]))}
    except Exception:
        return {}
    return {}


def fill_book_tags(audio: str, tags: Mapping[str, str],
                   written: list[str] | None = None) -> int:
    """Write any canonical audiobook tag in ``tags`` whose slot in ``audio`` is
    currently empty, leaving every non-empty tag untouched.

    Supports Opus, FLAC, Ogg, M4A/M4B/MP4 and MP3. Appends the canonical names
    actually written to ``written`` when given. Returns 0 on success (including
    when nothing needed writing), 1 on error or unsupported container."""
    lower = audio.lower()
    added: list[str] = []
    try:
        if lower.endswith(_VORBIS_SUFFIXES):
            handle = _open(audio)
            for field in BOOK_TAG_FIELDS:
                value = str(tags.get(field) or "").strip()
                if not value:
                    continue
                write_key, check_keys = _VORBIS_BOOK_TAGS[field]
                if _vorbis_read_field(handle, check_keys):
                    continue
                handle[write_key] = value
                added.append(field)
            if added:
                handle.save()
        elif lower.endswith(MP4_SUFFIXES):
            from mutagen.mp4 import MP4, MP4FreeForm

            mp4 = MP4(audio)
            if mp4.tags is None:
                mp4.add_tags()
            for field in BOOK_TAG_FIELDS:
                value = str(tags.get(field) or "").strip()
                if not value:
                    continue
                write_key, is_freeform, check_keys = _MP4_BOOK_TAGS[field]
                if _mp4_read_field(mp4, check_keys):
                    continue
                if is_freeform:
                    mp4[write_key] = [MP4FreeForm(value.encode("utf-8"))]
                else:
                    mp4[write_key] = [value]
                added.append(field)
            if added:
                mp4.save()
        elif lower.endswith(".mp3"):
            from mutagen.id3 import TDRC, TIT2, TLAN, TPE1, TXXX
            from mutagen.mp3 import MP3

            frames = {"TIT2": TIT2, "TPE1": TPE1, "TDRC": TDRC, "TLAN": TLAN}
            mp3 = MP3(audio)
            for field in BOOK_TAG_FIELDS:
                value = str(tags.get(field) or "").strip()
                if not value:
                    continue
                target_id, is_txxx, frame_checks, txxx_descs = _MP3_BOOK_TAGS[field]
                if _mp3_read_field(mp3.tags, frame_checks, txxx_descs):
                    continue
                if mp3.tags is None:
                    mp3.add_tags()
                id3 = mp3.tags
                if id3 is None:
                    return 1
                if is_txxx:
                    id3.add(TXXX(encoding=3, desc=target_id, text=[value]))
                else:
                    id3.add(frames[target_id](encoding=3, text=[value]))
                added.append(field)
            if added:
                mp3.save()
        else:
            return 1
    except Exception:
        return 1
    if written is not None:
        written.extend(added)
    return 0


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
    written or the existing ones won. Only an error is non-zero.
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
