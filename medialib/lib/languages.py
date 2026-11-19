"""The supported-language table and its lookups.

One place decides which languages this collection recognises and how each is
spelled in every place it turns up - subtitle suffixes, mkv tags, track names,
whisper output - so supporting another language is a row here and nothing else.
"""

from __future__ import annotations

import os
import re
from typing import NamedTuple

from medialib.lib import clioptions, titlematch
from medialib.lib.enums import shell_lower


class Language(NamedTuple):
    """One row of the table.

    ``code3b`` is ISO 639-2/B, which is what mkvmerge REPORTS where ``code3``
    (639-2/T) is what it is FED: a track muxed as "deu" reads back as "ger", so a
    lookup on the fed code alone would recognise no German, French or Dutch track
    at all. Only those three and Chinese have two codes (it is fed "chi" and
    reports "zho"); the rest repeat the one they have.
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
    Language("pt", "por", "Portuguese", ("portuguese", "português", "portugues"), "por"),
    Language("pl", "pol", "Polish", ("polish", "polski"), "pol"),
    Language("sv", "swe", "Swedish", ("swedish", "svenska"), "swe"),
    Language("da", "dan", "Danish", ("danish", "dansk"), "dan"),
    Language("no", "nor", "Norwegian", ("norwegian", "norsk"), "nor"),
    Language("fi", "fin", "Finnish", ("finnish", "suomi"), "fin"),
    Language("cs", "ces", "Czech", ("czech", "česky", "cesky"), "ces"),
    Language("hu", "hun", "Hungarian", ("hungarian", "magyar"), "hun"),
    Language("ro", "ron", "Romanian", ("romanian", "română", "romana"), "ron"),
    Language("el", "ell", "Greek", ("greek", "ελληνικά", "ellinika"), "ell"),
    Language("tr", "tur", "Turkish", ("turkish", "türkçe", "turkce"), "tur"),
    Language("ru", "rus", "Russian", ("russian", "русский"), "rus"),
    Language("ja", "jpn", "Japanese", ("japanese", "nihongo", "にほんご"), "jpn"),
    Language("ko", "kor", "Korean", ("korean", "hangugeo", "한국어"), "kor"),
    Language("zh", "chi", "Chinese", ("chinese", "zhongwen", "中文"), "zho"),
)

# The languages the user speaks when the ``spokenLanguages`` setting names
# none of its own: the table's first six, in the table's order - the
# languages ingest-movies fetched subtitles for before the setting existed,
# so a run that does not set it fetches what it always fetched.
SPOKEN_DEFAULT = ("en", "de", "fr", "nl", "es", "it")


def spoken_languages() -> tuple:
    """The languages the user speaks - the ones ingest-movies fetches and
    tests subtitles for, and the ones ytdlp takes a YouTube transcript in:
    the ``spokenLanguages`` setting's codes, in the order it names them, each
    as its row of :data:`LANGUAGES`.

    A setting that names nothing - unset or empty - names the default
    :data:`SPOKEN_DEFAULT`, a code named twice is one language, and a code
    the table does not know is refused, naming it and the codes the table
    has, because a language this collection does not recognise would be
    fetched for and read back with no one to have been told.
    """
    raw = os.environ.get("spokenLanguages", "")
    codes: list = []
    for entry in raw.split(","):
        code = shell_lower(entry.strip())
        if code and code not in codes:
            codes.append(code)
    if not codes:
        codes = list(SPOKEN_DEFAULT)
    by_code = {row.code2: row for row in LANGUAGES}
    unknown = [code for code in codes if code not in by_code]
    if unknown:
        raise clioptions.UsageError(
            "The spokenLanguages setting names %s, which the language "
            "table does not know. It knows: %s."
            % (", ".join('"%s"' % code for code in unknown),
               ", ".join(sorted(by_code))))
    return tuple(by_code[code] for code in codes)


# The words that mark an audio track as a commentary, in the spellings track names
# really use. A Matroska should say this with the commentary FLAG, and many do, but
# plenty of files only say it in the track NAME - and a name says it in the language
# of the disc it came from, so "Audiokommentar" is every bit as much a commentary as
# "Audio Commentary". Anything not recognised here gets no commentary flag and no
# transcript, so this list is what "every commentary track" means.
COMMENTARY_KEYWORDS = ("comment", "kommentar", "commentaar", "commentaire",
                       "comentario", "comentário", "commento", "komentarz",
                       "kommentointi", "komentář", "komentár", "kommentár",
                       "comentariu", "yorum", "комментарий", "σχόλιο",
                       "解説", "해설", "解说")

# mkvmerge reports an unset language as "und", and a missing property renders as the
# literal "null"; "mis", "qaa" and "zxx" are uncoded, reserved and "no linguistic
# content". None of them names a language.
_NOT_A_LANGUAGE = ("", "null", "und", "mis", "qaa", "zxx")


def is_real_language_tag(tag: str) -> bool:
    """Does this mkv language property actually say something?"""
    return shell_lower(tag) not in _NOT_A_LANGUAGE


# The commentary words themselves, whole, in the languages discs carry - what a
# track name spelled a letter wrong is measured against. "comment" is not among
# them: a word that short is a letter from far too many others, and the keyword
# above already finds it spelled right.
COMMENTARY_WORDS = ("commentary", "commentaries", "kommentar", "kommentare",
                    "commentaire", "commentaar", "comentario", "comentário",
                    "commento", "komentarz", "kommentointi", "komentář",
                    "kommentár", "comentariu", "yorum", "комментарий",
                    "σχόλιο", "解説", "해설", "解说")

# "audio" glued on in front, the way "Audiokommentar" is spelled.
_AUDIO_PREFIX = "audio"


def is_commentary_word(word: str) -> bool:
    """Whether one word of a track name is a commentary word, spelled a letter
    wrong or not: "Audiosommentary" and "Audiolkommentar" are.

    A slip is what it is for a film's title - one key mistyped, struck twice
    or missed, or two struck in the wrong order - and is read by the same
    rule, :func:`titlematch.one_slip_apart`, so the library holds one idea of
    a misspelling and not two.
    """
    word = shell_lower(word)
    if word.startswith(_AUDIO_PREFIX) and len(word) > len(_AUDIO_PREFIX):
        word = word[len(_AUDIO_PREFIX):]
    return any(word == target or titlematch.one_slip_apart(word, target)
               for target in COMMENTARY_WORDS)


def is_commentary_name(name: str) -> bool:
    """Does this track name mark it as a commentary? A keyword anywhere in it,
    or a word of it that is a commentary word spelled a letter wrong."""
    folded = shell_lower(name)
    if any(keyword in folded for keyword in COMMENTARY_KEYWORDS):
        return True
    return any(is_commentary_word(word)
               for word in re.findall(r"[^\W\d_]+", folded))


# The words a track name says it is for the deaf and hard of hearing with, in
# the languages discs carry. "cc" only as a word of its own, where it is
# closed captions and not two letters of something else.
_SDH_NAME = re.compile(r"\bsdh\b|\bcc\b|hearing|deaf|malentendant|"
                       r"h[oö]rgesch[aä]digt|doven|slechthorend", re.I)
# The words a track name says it is forced with.
_FORCED_NAME = re.compile(r"forced|forc[ée]s?\b|erzwungen|geforceerd", re.I)


def is_sdh_name(name: str) -> bool:
    """Does this track name mark it as subtitles for the deaf and hard of
    hearing?"""
    return bool(_SDH_NAME.search(name or ""))


def is_forced_name(name: str) -> bool:
    """Does this track name mark it as a forced subtitle - the signs and foreign
    lines, shown without being chosen?"""
    return bool(_FORCED_NAME.search(name or ""))


def without_forced_word(name: str) -> str:
    """A track name with the word that says it is forced taken out, and the
    brackets and dashes that are left around nothing with it."""
    rest = _FORCED_NAME.sub("", name or "")
    rest = re.sub(r"\(\s*\)|\[\s*\]", "", rest)
    return " ".join(rest.split()).strip(" -_,;:")


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


# The languages that are several to a precise tag and one to a catalogue or a
# disc, each under the one code they are compared by: the macrolanguage's
# ISO 639-1 code, then every spelling of it and of its members a track or TMDb
# may carry. Most Chinese discs tag Cantonese and Mandarin alike as "chi";
# precise ones say "yue" and "cmn", and TMDb says "cn" for Cantonese and "zh"
# for Mandarin, sometimes for a film that mixes both. Norwegian is "no" to
# TMDb and "nb" on most discs. Telling the members apart would drop a track
# for a distinction the tags make unevenly, so they count as one language.
LANGUAGE_FAMILIES = (
    ("zh", ("zh", "cn", "chi", "zho", "yue", "cmn", "nan", "hak", "wuu",
            "gan", "hsn", "cdo", "cjy", "cpx", "czh", "czo", "mnp", "lzh")),
    ("no", ("no", "nb", "nn", "nor", "nob", "nno")),
    ("sh", ("sh", "sr", "hr", "bs", "srp", "hrv", "bos", "scc", "scr", "cnr")),
    ("ar", ("ar", "ara", "arb", "arz", "apc", "ajp", "acm", "apd", "ary",
            "aeb", "afb", "ayl")),
    ("fa", ("fa", "fas", "per", "pes", "prs")),
    ("ms", ("ms", "msa", "may", "zsm")),
)


def _family(code: str) -> str:
    """The code a language is compared by: its family's, the table's
    two-letter code for one of the table's three-letter tags, or itself."""
    code = shell_lower(code)
    for family, members in LANGUAGE_FAMILIES:
        if code in members:
            return family
    return code_from_tag(code) or code


def track_language(language: str, ietf: str) -> str:
    """The language a track is spoken in, as :func:`speaks` compares it, or ""
    for a track whose tags name none.

    ``ietf`` is the BCP 47 tag mkvmerge reports beside the legacy
    ``language`` and leads with the two-letter subtag wherever one exists,
    which is what makes any language comparable rather than only the
    table's; a file without it is read by its legacy tag.
    """
    subtag = shell_lower(ietf).split("-")[0]
    if is_real_language_tag(subtag):
        return _family(subtag)
    if is_real_language_tag(language):
        return _family(language)
    return ""


def speaks(language: str, ietf: str, code: str) -> bool:
    """Is a track spoken in the language a catalogue names by ``code``?

    ``code`` is ISO 639-1, as TMDb writes a film's languages; a language of
    :data:`LANGUAGE_FAMILIES` is matched by any of its members.
    """
    spoken = track_language(language, ietf)
    return bool(spoken) and bool(code) and spoken == _family(code)


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
