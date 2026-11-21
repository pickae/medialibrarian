"""Languages: what a code, a tag or a name stands for, and which ones this
collection reads track names for.

The codes and the English names are ISO 639 and come from ``pycountry``, so
any language ISO 639-1 has is a language here - nothing is typed per
language but the three short lists below, which are choices rather than data:
the languages whose names are looked for in a track's name, the commentary
words, and the macrolanguage families.
"""

from __future__ import annotations

import functools
import gettext
import os
import re
import unicodedata
from typing import NamedTuple

from medialib.lib import clioptions, settings, titlematch
from medialib.lib.enums import shell_lower


class Language(NamedTuple):
    """One language.

    ``code3b`` is ISO 639-2/B, which is what mkvmerge REPORTS where ``code3``
    (639-2/T) is what it is FED: a track muxed as "deu" reads back as "ger", so a
    lookup on the fed code alone would recognise no German, French or Dutch track
    at all. Where the language has one code of the two the field repeats it.

    ``sub_word`` is the language's English name as whisper and the subtitle
    providers spell it ("Greek", not ISO's "Modern Greek (1453-)").
    ``keywords`` are its names a track may carry, lower case, as written and
    with the accents off - in English, in the language itself and in the
    languages the user speaks.
    """

    code2: str
    code3: str
    sub_word: str
    keywords: tuple
    code3b: str


# The languages whose names are looked for in a track's name, in priority
# order: where a name holds two languages' words, the first of them wins.
# Every language the user names in ``spokenLanguages`` or ``keepLanguages``
# is looked for too, after these. A language outside both is still
# understood by its code, in a tag or a sidecar's name - it is only that its
# name, which a short one makes a likely stranger inside a longer word, is
# not searched for.
NAMED = ("en", "de", "fr", "nl", "es", "it", "pt", "pl", "sv", "da", "no",
         "fi", "cs", "hu", "ro", "el", "tr", "ru", "ja", "ko", "zh")

# Names a language is written as that ISO's translations do not carry: the
# Latin spelling of an endonym a disc writes in the Latin alphabet, or the
# one word a language has for itself that its name in ISO's own list is not.
_EXTRA_NAMES = {
    "cs": ("cesky",),
    "el": ("ellinika", "ελληνικά", "griechisch", "grieks", "griego", "grec",
           "greco"), "ja": ("nihongo",),
    "ko": ("hangugeo",), "zh": ("zhongwen",), "no": ("norsk",),
}


def _fold_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@functools.cache
def _by_tag() -> dict:
    """Every spelling of every ISO 639 code that has a two-letter one, to
    its language."""
    import pycountry
    found = {}
    for entry in pycountry.languages:
        if not getattr(entry, "alpha_2", ""):
            continue
        for spelling in (entry.alpha_2, entry.alpha_3,
                         getattr(entry, "bibliographic", "")):
            if spelling:
                found[spelling] = entry
    return found


def _plain_name(name: str) -> str:
    """ISO's name for a language as people write it: "Modern Greek
    (1453-)" is "Greek", "Spanish; Castilian" is "Spanish"."""
    name = re.sub(r"\s*\(.*?\)", "", name)
    name = re.split(r"[;,]", name)[0].strip()
    return name[len("Modern "):] if name.startswith("Modern ") else name


@functools.cache
def _catalogue(locale: str):
    """ISO 639's name list translated into ``locale``, or None where
    pycountry has none. A language with only regional translations (Norwegian
    has nb_NO and nn, Chinese zh_CN) takes the first it has."""
    import pycountry
    found = sorted(name for name in os.listdir(pycountry.LOCALES_DIR)
                   if name == locale or name.startswith(locale + "_")
                   or (locale == "no" and name in ("nb_NO", "nn")))
    for name in found:
        try:
            return gettext.translation("iso639-3", pycountry.LOCALES_DIR,
                                       languages=[name])
        except OSError:
            continue
    return None


def _names(entry, locales: tuple) -> tuple:
    """The names for ``entry``: its English name, its name in itself and in
    each of ``locales``, written as found and with the accents off. A short
    Latin-script word is left out - it is more likely a stranger inside a
    longer one than the language."""
    names = {_plain_name(entry.name)}
    for locale in (entry.alpha_2, *locales):
        catalogue = _catalogue(locale)
        if catalogue is not None:
            names.add(_plain_name(catalogue.gettext(entry.name)))
    names.update(_EXTRA_NAMES.get(entry.alpha_2, ()))
    found = set()
    for name in names:
        lowered = shell_lower(name)
        plain = _fold_accents(lowered)
        if not lowered.isascii():
            found.add(lowered)
        if plain.isascii() and len(plain) >= 4:
            found.add(plain)
    return tuple(sorted(found))


@functools.cache
def _language(code2: str, locales: tuple) -> Language | None:
    entry = _by_tag().get(code2)
    if entry is None or entry.alpha_2 != code2:
        return None
    return Language(code2, entry.alpha_3, _plain_name(entry.name),
                    _names(entry, locales),
                    getattr(entry, "bibliographic", "") or entry.alpha_3)


def language(code: str) -> Language | None:
    """The language an ISO 639-1 code names, or None where ISO has none."""
    return _language(shell_lower(code), _locales())


def _locales() -> tuple:
    """The languages whose names a track's language may be written in: those
    the user speaks. Read from the setting each time - it may have been
    loaded after this module was."""
    return _setting_codes("spokenLanguages")


def _setting_codes(name: str) -> tuple:
    """The two-letter codes a language setting names, in its order, a code
    named twice once. UsageError for one ISO does not know, naming it."""
    codes: list = []
    for entry in settings.value(name).split(","):
        code = shell_lower(entry.strip())
        if code and code not in codes:
            codes.append(code)
    unknown = [code for code in codes if code not in _by_tag()
               or _by_tag()[code].alpha_2 != code]
    if unknown:
        raise clioptions.UsageError(
            "The %s setting names %s, which ISO 639-1 does not know."
            % (name, ", ".join('"%s"' % code for code in unknown)))
    return tuple(codes)


def setting_languages(name: str) -> tuple:
    """The languages a setting names, as :class:`Language` rows, in its
    order. Unset or empty is the shipped value (see
    :func:`medialib.lib.settings.value`)."""
    return tuple(language(code) for code in _setting_codes(name))


def spoken_languages() -> tuple:
    """The languages the user speaks - the ones ingest-movies fetches and
    tests subtitles for, and the ones ytdlp takes a YouTube transcript in:
    the ``spokenLanguages`` setting's languages, in its order."""
    return setting_languages("spokenLanguages")


def recognised() -> tuple:
    """The languages a track's name is searched for: :data:`NAMED`, then
    whatever the user's settings name that it does not."""
    codes = list(NAMED)
    for name in ("spokenLanguages", "keepLanguages"):
        codes += [code for code in _setting_codes(name) if code not in codes]
    return tuple(language(code) for code in codes)


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


def _iso2(tag: str) -> str:
    """The two-letter code of any ISO 639 spelling of any language, or ""."""
    folded = shell_lower(tag)
    if not is_real_language_tag(folded):
        return ""
    entry = _by_tag().get(folded)
    return entry.alpha_2 if entry else ""


def code_from_tag(tag: str) -> str:
    """The ".xx" code for an mkv language tag of a language this collection
    recognises (:func:`recognised`), or "" for any other.

    Every spelling ISO gives the language is accepted: the two-letter one,
    which is also what mkvmerge reports as language_ietf, and both
    three-letter ones. It is the recognised languages and not all of ISO's
    because the answer is also asked of the last word of a file name, where
    ISO's "ii" and "iii" - Sichuan Yi - are a film's roman numerals.
    """
    code = _iso2(tag)
    return code if code in {row.code2 for row in recognised()} else ""


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
    return _iso2(code) or code


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
    """Do two mkv language tags name one language?

    Whatever spelling each turns up in - two-letter, 639-2/T or 639-2/B (see
    ``code_from_tag`` for why all three exist). Tags that name no ISO language
    (or no language at all) only match when spelled
    identically.
    """
    code_a = _iso2(tag_a)
    if code_a:
        return code_a == _iso2(tag_b)
    return shell_lower(tag_a) == shell_lower(tag_b)


@functools.cache
def _by_name() -> dict:
    return {shell_lower(_plain_name(entry.name)): entry.alpha_2
            for entry in _by_tag().values()}


def code_from_name(name: str) -> str:
    """The ".xx" code for the English name of a language this collection
    recognises, or "" for any other: a language it does not recognise is
    handed to whisper by its name, and no transcript is written for it.

    The names are exactly the spelling whisper-ctranslate2 prints - "Detected
    language 'Dutch'" - because it title-cases the same English names.
    """
    code = _by_name().get(shell_lower(name), "")
    return code if code in {row.code2 for row in recognised()} else ""


def code3_from_label(label: str) -> str:
    """The ISO 639-3 code of the language a tag or a name stands for, or "".

    ``label`` is whatever a file says: a two- or three-letter code of any
    ISO spelling, a member of one of :data:`LANGUAGE_FAMILIES` ("cmn" is
    Chinese), the English name ("German") or a name the language is called
    by on a disc ("deutsch").
    """
    folded = shell_lower(label)
    code2 = _iso2(folded) or _by_name().get(folded, "") or next(
        (row.code2 for row in recognised() if folded in row.keywords), "")
    if not code2:
        family = _family(folded)
        code2 = family if family in _by_tag() else ""
    entry = _by_tag().get(code2)
    return entry.alpha_3 if entry else ""
