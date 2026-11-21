"""What an audio track is: the role it plays, one of :data:`enums.AUDIO_ROLES`.

Read the same way wherever a command looks at an audio track - from mkvmerge's
report or ffprobe's - so a track one step calls an audio description is not a
soundtrack to the next.
"""

from __future__ import annotations

import re

from medialib.lib import aitrackrole, languages

__all__ = ["audio_role", "refine_audio_role", "is_main"]

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
               visual_impaired: bool = False,
               language: str = "", transcript: str = "",
               main_transcript: str = "", unsure: bool = False,
               models=None, meter_file: str = "",
               allow_llm: bool = False) -> str:
    """Which of :data:`enums.AUDIO_ROLES` an audio track plays.

    The container's flag says it where one is set - the commentary flag, and
    the visually-impaired one an audio description carries - and the track's
    name where none is. The roles are asked in their order, so a track that is
    two of them is the first.

    Where neither flags nor known name patterns match and ``allow_llm`` is set
    (or a ``transcript`` / ``unsure`` acoustic verdict is supplied), a light
    model may be asked on borderline or unrecognised cases
    (:mod:`medialib.lib.aitrackrole`).
    """
    name = name or ""
    if commentary or languages.is_commentary_name(name):
        return "commentary"
    if visual_impaired:
        return "description"
    for role, pattern in _NAMED:
        if pattern.search(name):
            return role
    if allow_llm or transcript or unsure:
        return aitrackrole.resolve_audio_role(
            "main",
            name=name,
            language=language,
            flags={"commentary": commentary, "visual_impaired": visual_impaired},
            transcript=transcript,
            main_transcript=main_transcript,
            unsure=unsure,
            flagged=False,
            models=models,
            meter_file=meter_file,
        )
    return "main"


def refine_audio_role(role: str, *, name: str = "", language: str = "",
                      transcript: str = "", main_transcript: str = "",
                      speech: float | None = None,
                      main_speech: float | None = None,
                      new_words: float | None = None,
                      unsure: bool = False, flagged: bool = False,
                      models=None, meter_file: str = "",
                      allow_llm: bool = True) -> str:
    """Refine a deterministic audio track ``role`` when acoustic checks were
    borderline, inconclusive, or unavailable, or the title is an unrecognised
    descriptive label (:mod:`medialib.lib.aitrackrole`).

    Tracks already settled by a container flag or a known non-main role are
    never sent to the model.
    """
    if role != "main" or flagged or not allow_llm:
        return role
    return aitrackrole.resolve_audio_role(
        role,
        name=name,
        language=language,
        transcript=transcript,
        main_transcript=main_transcript,
        speech=speech,
        main_speech=main_speech,
        new_words=new_words,
        unsure=unsure,
        flagged=flagged,
        models=models,
        meter_file=meter_file,
    )


def is_main(name: str, *, commentary: bool = False,
            visual_impaired: bool = False,
            language: str = "", transcript: str = "",
            main_transcript: str = "", unsure: bool = False,
            models=None, meter_file: str = "",
            allow_llm: bool = False) -> bool:
    """Whether the track is a soundtrack to watch the film in."""
    return audio_role(name, commentary=commentary,
                      visual_impaired=visual_impaired,
                      language=language, transcript=transcript,
                      main_transcript=main_transcript, unsure=unsure,
                      models=models, meter_file=meter_file,
                      allow_llm=allow_llm) == "main"
