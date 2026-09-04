"""The supported-language table and its lookups.

One place decides which languages this collection recognises and how each is
spelled in every place it turns up - subtitle suffixes, mkv tags, track names,
whisper output - so supporting another language is a row here and nothing else.
"""

from __future__ import annotations

from typing import NamedTuple

from medialib.lib.enums import shell_lower


class Language(NamedTuple):
    """One row of the table.

    ``code3b`` is ISO 639-2/B, which is what mkvmerge REPORTS where ``code3``
    (639-2/T) is what it is FED: a track muxed as "deu" reads back as "ger", so a
    lookup on the fed code alone would recognise no German, French or Dutch track
    at all. Only those three have two codes; the rest repeat the one they have.
    """

    code2: str
    code3: str
    sub_word: str
    keywords: tuple
    code3b: str


# In priority order. ``keywords`` holds language words ONLY, never a commentary
# marker: it says what a track is spoken IN, not what it is. A "commentary" keyword
# in the English row would stamp "eng" over every foreign commentary's language tag
# and rob the transcription step of its one hint about what to transcribe.
LANGUAGES = (
    Language("en", "eng", "English", ("english", "englisch", "anglais", "ingles"), "eng"),
    Language("de", "deu", "German", ("german", "deutsch", "allemand"), "ger"),
    Language("fr", "fra", "French", ("french", "français", "francais", "französisch"), "fre"),
    Language("nl", "nld", "Dutch", ("dutch", "nederlands"), "dut"),
    Language("es", "spa", "Spanish", ("spanish", "espagnol"), "spa"),
    Language("it", "ita", "Italian", ("italian", "italien"), "ita"),
)

# The words that mark an audio track as a commentary, in the spellings track names
# really use. A Matroska should say this with the commentary FLAG, and many do, but
# plenty of files only say it in the track NAME - and a name says it in the language
# of the disc it came from, so "Audiokommentar" is every bit as much a commentary as
# "Audio Commentary". Anything not recognised here gets no commentary flag and no
# transcript, so this list is what "every commentary track" means.
COMMENTARY_KEYWORDS = ("comment", "kommentar", "commentaar", "commentaire",
                       "comentario", "commento")

# mkvmerge reports an unset language as "und", and a missing property renders as the
# literal "null"; "mis", "qaa" and "zxx" are uncoded, reserved and "no linguistic
# content". None of them names a language.
_NOT_A_LANGUAGE = ("", "null", "und", "mis", "qaa", "zxx")


def is_real_language_tag(tag: str) -> bool:
    """Does this mkv language property actually say something?"""
    return shell_lower(tag) not in _NOT_A_LANGUAGE


def is_commentary_name(name: str) -> bool:
    """Does this track name mark it as a commentary? Lower-case substrings."""
    folded = shell_lower(name)
    return any(keyword in folded for keyword in COMMENTARY_KEYWORDS)


def code_from_tag(tag: str) -> str:
    """The ".xx" code for an mkv language tag, or "" for a tag naming none.

    Every spelling of the table's codes is accepted: the two-letter one, which is
    also what mkvmerge reports as language_ietf, and both three-letter ones.
    """
    folded = shell_lower(tag)
    if not is_real_language_tag(folded):
        return ""
    for row in LANGUAGES:
        if folded in (row.code2, row.code3, row.code3b):
            return row.code2
    return ""


def same_language_tag(tag_a: str, tag_b: str) -> bool:
    """``sameLanguageTag``: do two mkv language tags name one language?

    Whatever spelling each turns up in - two-letter, 639-2/T or 639-2/B (see
    ``code_from_tag`` for why all three exist). Tags that name no table row
    (foreign languages, or no language at all) only match when spelled
    identically.
    """
    code_a = code_from_tag(tag_a)
    if code_a:
        return code_a == code_from_tag(tag_b)
    return shell_lower(tag_a) == shell_lower(tag_b)


def code_from_name(name: str) -> str:
    """The ".xx" code for an English language name, or "" for one with no row.

    The names are exactly the spelling whisper-ctranslate2 prints - "Detected
    language 'Dutch'" - because it title-cases the same English names.
    """
    folded = shell_lower(name)
    for row in LANGUAGES:
        if shell_lower(row.sub_word) == folded:
            return row.code2
    return ""
