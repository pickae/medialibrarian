"""What a subtitle track is: its form, and the role it plays.

The form is how it is written - text a program can read, or pictures only OCR
can - and the role is what it is for, one of :data:`enums.SUBTITLE_ROLES`. Both
are read the same way wherever a command looks at a subtitle, from mkvmerge's
report or ffprobe's, so one track is never two things to two steps of a run.
"""

from __future__ import annotations

from medialib.lib import enums, languages

__all__ = ["subtitle_form", "subtitle_role", "sidecar_name"]


def subtitle_form(codec: str) -> str:
    """``"text"``, ``"image"``, or ``""`` for a codec that is no subtitle.

    Either spelling of the codec: mkvmerge's codec_id ("S_HDMV/PGS",
    "S_TEXT/UTF8") or ffprobe's codec_name ("hdmv_pgs_subtitle", "subrip").
    """
    folded = enums.shell_lower(codec or "")
    if (folded in enums.IMAGE_SUBTITLE_TRACK_CODECS
            or folded in enums.IMAGE_SUBTITLE_CODECS):
        return "image"
    if (folded.startswith(enums.TEXT_SUBTITLE_TRACK_PREFIX)
            or folded in enums.TEXT_SUBTITLE_CODECS):
        return "text"
    return ""


def subtitle_role(name: str, *, forced: bool = False,
                  hearing_impaired: bool = False,
                  commentary: bool = False) -> str:
    """Which of :data:`enums.SUBTITLE_ROLES` a subtitle track plays.

    The container's flag says it where one is set, and the track's name where
    none is - a disc's tracks are often named and not flagged. The roles are
    asked in their order, so a track that is two of them is the first.
    """
    name = name or ""
    if commentary or languages.is_commentary_name(name):
        return "commentary"
    if forced or languages.is_forced_name(name):
        return "forced"
    if hearing_impaired or languages.is_sdh_name(name):
        return "sdh"
    return "full"


def sidecar_name(stem: str, language_code: str, role: str = "full") -> str:
    """The ``.srt`` a film's subtitle of one language and role is written to
    beside it: ``<stem>.<xx>.srt``, or ``<stem>.<xx>.forced.srt`` and
    ``<stem>.<xx>.sdh.srt``.

    A commentary has no name here: its sidecar is named after the commentary.
    """
    suffix = enums.SUBTITLE_ROLE_SUFFIXES[role]
    tag = "{}.{}".format(language_code, suffix) if suffix else language_code
    return "{}.{}.srt".format(stem, tag)
