"""What an audio track is: the role it plays, one of :data:`enums.AUDIO_ROLES`.

Read the same way wherever a command looks at an audio track - from mkvmerge's
report or ffprobe's - so a track one step calls an audio description is not a
soundtrack to the next.
"""

from __future__ import annotations

import re

from medialib.lib import languages

__all__ = ["audio_role", "is_main"]

# The words a track name says each role with, in the languages discs carry.
#
# "Audio Description", "audiodescription", "Audiodeskription", "AD" - the last
# in capitals only, where a lower-case "ad" is a word in more than one language.
_DESCRIPTION_NAME = re.compile(r"(?i:descri|deskri)|\bAD\b")
# "Music & Effects", "M&E", "M & E".
_EFFECTS_NAME = re.compile(r"effects|\bm ?& ?e\b", re.IGNORECASE)
# "Isolated Score", "Score only", "Music only", "Musik", "Musique".
_SCORE_NAME = re.compile(r"isolat|\bscore\b|\bmusi[ck]\b|\bmusique\b", re.IGNORECASE)
_INTERVIEW_NAME = re.compile(r"interview", re.IGNORECASE)

_NAMED = (("description", _DESCRIPTION_NAME), ("effects", _EFFECTS_NAME),
          ("score", _SCORE_NAME), ("interview", _INTERVIEW_NAME))


def audio_role(name: str, *, commentary: bool = False,
               visual_impaired: bool = False) -> str:
    """Which of :data:`enums.AUDIO_ROLES` an audio track plays.

    The container's flag says it where one is set - the commentary flag, and
    the visually-impaired one an audio description carries - and the track's
    name where none is. The roles are asked in their order, so a track that is
    two of them is the first.
    """
    name = name or ""
    if commentary or languages.is_commentary_name(name):
        return "commentary"
    if visual_impaired:
        return "description"
    for role, pattern in _NAMED:
        if pattern.search(name):
            return role
    return "main"


def is_main(name: str, *, commentary: bool = False,
            visual_impaired: bool = False) -> bool:
    """Whether the track is a soundtrack to watch the film in."""
    return audio_role(name, commentary=commentary,
                      visual_impaired=visual_impaired) == "main"
