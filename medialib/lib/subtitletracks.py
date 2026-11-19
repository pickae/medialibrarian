"""What a subtitle track is: its form, and the role it plays.

The form is how it is written - text a program can read, or pictures only OCR
can - and the role is what it is for, one of :data:`enums.SUBTITLE_ROLES`. Both
are read the same way wherever a command looks at a subtitle, from mkvmerge's
report or ffprobe's, so one track is never two things to two steps of a run.
"""

from __future__ import annotations

import re

from medialib.lib import enums, languages

__all__ = ["subtitle_form", "subtitle_role", "sidecar_name", "refine_role",
           "is_sdh_text", "cue_texts", "MIN_REFERENCE_CUES",
           "MAX_REFERENCE_CUES"]

# The fewest cues a subtitle needs to stand for a film's whole dialogue: one
# with fewer says too little to measure another track against.
MIN_REFERENCE_CUES = 100

# The most: the longest dialogue of a long film is some 3500 lines, and a
# picture subtitle counted at several times that redraws its lines packet by
# packet, which says nothing of how many lines it has.
MAX_REFERENCE_CUES = 5000

_SDH_CUE = re.compile("|".join(enums.SDH_CUE_MARKERS), re.M)


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


def is_sdh_text(cues) -> bool:
    """Whether a subtitle's cues, as their text, read as SDH: enough of them
    carry a sound, a note of music or a speaker's name - see
    :data:`enums.SDH_CUE_MARKERS`."""
    cues = [cue for cue in cues if cue.strip()]
    if not cues:
        return False
    marked = sum(1 for cue in cues if _SDH_CUE.search(cue))
    return marked >= enums.SDH_CUE_SHARE * len(cues)


def refine_role(role: str, cues: int = 0, reference_cues: int = 0,
                texts=None) -> str:
    """A track's role from its flags and name (:func:`subtitle_role`),
    corrected by what it holds.

    ``cues`` is how many lines the track has, ``reference_cues`` how many the
    longest subtitle of the film has, 0 for unknown; ``texts`` its cues' text,
    where it has been read. A commentary is what it says it is. A forced track
    as long as the dialogue is the whole of it, and a full one far shorter is
    forced (:data:`enums.FORCED_CUE_SHARE`, :data:`enums.FULL_CUE_SHARE`) -
    measured only against a reference that can stand for the film's dialogue
    (:data:`MIN_REFERENCE_CUES`, :data:`MAX_REFERENCE_CUES`). A full track
    whose text is SDH (:func:`is_sdh_text`) is SDH.
    """
    if role == "commentary":
        return role
    measured = (cues > 0
                and MIN_REFERENCE_CUES <= reference_cues <= MAX_REFERENCE_CUES)
    if role == "forced":
        if measured and cues >= enums.FULL_CUE_SHARE * reference_cues:
            return "full"
        return role
    if measured and cues < enums.FORCED_CUE_SHARE * reference_cues:
        return "forced"
    if role == "full" and texts is not None and is_sdh_text(texts):
        return "sdh"
    return role


def cue_texts(srt: str) -> list:
    """The text of each cue of a SubRip file, its lines joined by newlines;
    an unreadable file has none."""
    try:
        with open(srt, encoding="utf-8-sig", errors="replace") as handle:
            blocks = handle.read().replace("\r\n", "\n").split("\n\n")
    except OSError:
        return []
    texts = []
    for block in blocks:
        lines = block.strip("\n").split("\n")
        for at, line in enumerate(lines):
            if "-->" in line:
                texts.append("\n".join(lines[at + 1:]))
                break
    return texts
