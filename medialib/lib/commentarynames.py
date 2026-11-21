"""Which of a film's commentaries is which, read from a list of a disc's extras.

A rip often names its commentary tracks "Commentary 1", "Commentary 2" - the disc
said which was which only in its menu. A database of disc releases lists each
release's commentaries by who is speaking, and this module decides whether that
list can be put on the tracks. It refuses whenever it cannot be sure, because a
track given the wrong people's names is worse than one given a number.

Three things have to hold before a name is given:

- every commentary track of the film is still only numbered, and the database
  lists exactly as many commentaries for the kind of disc the file was made from
  as the file has;
- every such release agrees on what they are, or the transcripts pick out one
  set and one only;
- for two or more, which track is which is proved by the transcripts, and the
  listing's order counts for nothing. A database lists the commentaries in its
  menu's order, and nothing makes a disc's audio streams follow its menu - but
  commentators nearly always open by saying who they are, so each track's
  opening minutes have to name one entry's people, and nobody else's, and
  exactly one way of laying the entries on the tracks may fit. One commentary
  whose people never say who they are may take the track left over.

Nothing here reads a file or the network: the caller hands over the tracks, the
releases and the transcripts' text, and is handed back a plan or a reason.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from itertools import permutations
from typing import NamedTuple

from medialib.lib import commentarytranscription, languages, plexnames, resolutions

__all__ = [
    "Disc",
    "Release",
    "FileCommentary",
    "CommentaryGroup",
    "Decision",
    "INTRO_SECONDS",
    "INTRO_SAMPLE_SECONDS",
    "TEXT_SUBTITLE_EXTENSIONS",
    "generic_number",
    "disc_format",
    "order_file_commentaries",
    "participants",
    "opening_text",
    "opening_subtitle_text",
    "intro_mentions",
    "track_title",
    "decide",
    "generic_groups",
    "subtitle_renames",
    "sidecar_renames",
    "group_sidecar_renames",
]


class Disc(NamedTuple):
    """One disc of a release: what kind of disc it is ("uhd", "bluray",
    "dvd"), and the audio commentaries on its main feature in the order the
    release lists them."""

    format: str
    commentaries: tuple[str, ...]


class Release(NamedTuple):
    """One release of a film - a country's edition, a box set - as the database
    lists it. ``label`` is only ever said back, never matched on."""

    label: str
    discs: tuple[Disc, ...]


class FileCommentary(NamedTuple):
    """One commentary audio track of the file, as the caller read it: its
    mkvmerge id and its name."""

    id: str
    name: str


class CommentaryGroup(NamedTuple):
    """Equivalent generically named commentary surfaces of one film - its
    audio track (if present), any embedded commentary subtitle translations,
    and any sidecar commentary files (including multiple translations) - so
    they are analysed once and renamed together."""

    key: str
    number: int
    label: str
    audio_ids: tuple[str, ...]
    subtitle_ids: tuple[str, ...]
    sidecars: tuple[str, ...]


class Decision(NamedTuple):
    """What to do with one film: ``names`` pairs each commentary track id with
    the name it is to be given, empty when ``reason`` says why nothing is.
    ``model_note`` records what a language model decided when one was asked."""

    names: tuple[tuple[str, str], ...]
    reason: str
    source: str = ""
    model_note: str = ""


# --- the file's own commentary tracks ---------------------------------------

@functools.cache
def _filler_words() -> frozenset:
    """Words a numbered name carries beside the commentary word and the
    number, and which say nothing about who is speaking: the language,
    "audio", "track". Built on first use, once the settings that pick the
    languages are loaded."""
    rows = languages.recognised()
    return frozenset(
        ["audio", "track", "spur", "piste", "no", "nr", "number", "num", "the"]
        + [keyword for row in rows for keyword in row.keywords]
        + [row.sub_word.lower() for row in rows]
        + [row.code2 for row in rows]
        + [row.code3 for row in rows])

def _fold(text: str) -> str:
    """Lower case without accents, so "Kommentär" and "Kommentar" are one
    word and a transcript's "Zoe" meets a listing's "Zoë"."""
    decomposed = unicodedata.normalize("NFKD", text)
    kept = "".join(c for c in decomposed if not unicodedata.combining(c))
    return kept.casefold()


def generic_number(name: str) -> int | None:
    """The number a track name that says nothing but "a commentary" gives it -
    0 for none at all - or None for a name that says more than that.

    "Commentary", "Commentary 2", "Audio Commentary #3", "English commentary
    track 1", "Kommentar 2", "Audiolkommentar" and "commentary (english)" are
    all this, and so is each spelled a letter wrong ("Audiosommentary 2",
    :func:`languages.is_commentary_word`); "Director's Commentary" is not,
    and neither is anything with somebody's name in it.
    """
    words = re.findall(r"[^\W_]+", _fold(name or ""))
    commentary = number = 0
    for word in words:
        if word.isdigit():
            if number or len(word) > 2:
                return None
            number = int(word)
        elif languages.is_commentary_word(word):
            commentary += 1
        elif word not in _filler_words():
            return None
    if commentary != 1:
        return None
    return number


def disc_format(width: object, height: object) -> str:
    """Which kind of disc a file of this size was made from: "uhd", "bluray"
    or "dvd", "" when the size could not be read.

    A 1440p file is a Blu-ray one: nothing is mastered at that size, and a
    Blu-ray cropped or scaled is the likeliest way to arrive at it.
    """
    tier = resolutions.tier_of(width, height)
    if tier in ("2160p", "4320p"):
        return "uhd"
    if tier in ("720p", "1080p", "1440p"):
        return "bluray"
    if tier == resolutions.SUB_TIER:
        return "dvd"
    return ""


def order_file_commentaries(tracks: list) -> tuple[list, str]:
    """The film's commentary tracks in the order a listing is to be read onto
    them, or ([], why not).

    Only a film whose every commentary is still only numbered is one to name:
    a track somebody already named has an answer, and a film with one named
    and one numbered is a film somebody was part way through. Numbered tracks
    are ordered by their numbers, which have to run 1..n; unnumbered ones by
    where they stand in the file.
    """
    if not tracks:
        return [], "no commentary track"
    read = [generic_number(track.name) for track in tracks]
    if any(number is None for number in read):
        return [], "a commentary track already has a name"
    numbers = [number or 0 for number in read]
    if all(numbers):
        if sorted(numbers) != list(range(1, len(tracks) + 1)):
            return [], ("the commentary tracks are numbered %s, not 1 to %d"
                        % (", ".join(str(n) for n in numbers), len(tracks)))
        return [track for _n, track in sorted(zip(numbers, tracks,
                                                  strict=True),
                                              key=lambda pair: pair[0])], ""
    if any(numbers):
        return [], "some commentary tracks are numbered and some are not"
    return list(tracks), ""


# --- who is speaking ---------------------------------------------------------

# Capitalised words in a listing that are not anybody's name: what the
# commentary is, what the people in it did, and the small words between.
_NOT_NAMES_WORDS = """
    audio commentary commentaries commentator commentators track tracks feature
    film films movie movies picture scene scenes select selected optional new
    archival archive original previously released included exclusive isolated
    score music with by from and featuring feat plus also the a an of on in for
    to at as his her their its director directors co writer writers
    screenwriter screenwriters producer producers executive associate actor
    actors actress actresses star stars cast crew member members filmmaker
    filmmakers cinematographer cinematographers photography editor editors
    composer composers designer designers production costume makeup visual
    special effects supervisor supervisors historian historians critic critics
    scholar scholars expert experts author authors journalist journalists
    novelist creator creators host hosted moderated moderator moderators
    critics team group panel stunt coordinator animator animators artist
    artists sound mixer engineer consultant technical adviser advisor friend
    friends family son daughter wife husband brother sister mr mrs ms dr sir
    dame professor prof jr sr english german french spanish italian dutch
    japanese subtitles subtitled dubbed version cut theatrical extended
    directors edition release restored restoration remastered blu ray dvd uhd
    """
_NOT_NAMES = frozenset(_NOT_NAMES_WORDS.split())

# Words that begin somebody saying who is there, in the first minutes of a
# commentary: "I'm", "my name is", "this is", "here with", "joined by".
_INTRO_PHRASES = (("i", "m"), ("i", "am"), ("my", "name", "is"),
                  ("name", "s"), ("this", "is"), ("here", "with"),
                  ("joined", "by"), ("with", "me"), ("with", "us"),
                  ("along", "with"), ("hi",), ("hello",), ("and", "i"),
                  ("we", "have"), ("i", "have"))

# How many words after such an opening can still be the name it introduces:
# "I'm the director, John Smith" puts the name four words on.
_INTRO_REACH = 6

# The opening minutes a commentary says who is in it: generous, since some let
# the film's opening play before they speak.
INTRO_SECONDS = 600

# The opening window sampled when naming a generic commentary directly from
# speaker introductions in its subtitles or transcribed audio: first 5 minutes.
INTRO_SAMPLE_SECONDS = 300

# Text subtitle extensions whose cues can be read directly without OCR.
TEXT_SUBTITLE_EXTENSIONS = ("srt", "vtt", "ass", "ssa")


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", _fold(text))


def participants(description: str, title: str = "") -> frozenset[str]:
    """The words of a listed commentary that are somebody's name, folded.

    A capitalised word is a name unless it is one of the words a listing puts
    around names, or a word of the film's own title - a listing that says
    "commentary on Nightfall" has not named anyone called Nightfall. Words
    shorter than three letters say too little to find again.
    """
    title_words = set(_words(title))
    found = set()
    for word in re.findall(r"[^\W\d_]+", description or ""):
        if not word[0].isupper():
            continue
        folded = _fold(word)
        if len(folded) < 3 or folded in _NOT_NAMES or folded in title_words:
            continue
        found.add(folded)
    return frozenset(found)


_SRT_TIME = re.compile(r"(?:(\d+):)?(\d\d):(\d\d)[,.](\d+)\s*-->")
_ASS_DIALOGUE = re.compile(
    r"^Dialogue:\s*\d+\s*,\s*(\d+):(\d\d):(\d\d)[.,]\d+\s*,"
    r"\s*\d+:\d\d:\d\d[.,]\d+\s*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,(.*)$")
_ASS_TAGS = re.compile(r"\{[^}]*\}")
_HTML_TAGS = re.compile(r"<[^>]+>")


def opening_text(srt: str, limit: int = INTRO_SECONDS) -> str:
    """The spoken lines from cues starting within ``limit`` seconds of an SRT
    or WebVTT transcript, one line per cue line."""
    text = []
    start = None
    for line in (srt or "").splitlines():
        stamp = _SRT_TIME.match(line.strip())
        if stamp:
            hours = int(stamp.group(1) or 0)
            minutes, seconds = int(stamp.group(2)), int(stamp.group(3))
            start = hours * 3600 + minutes * 60 + seconds
            continue
        if start is None or start >= limit:
            continue
        stripped = line.strip()
        if not stripped or stripped.isdigit() or stripped.upper() == "WEBVTT":
            continue
        cleaned = _HTML_TAGS.sub("", stripped).strip()
        if cleaned:
            text.append(cleaned)
    return "\n".join(text)


def opening_subtitle_text(text: str,
                          limit: int = INTRO_SAMPLE_SECONDS) -> str:
    """Spoken lines from the first ``limit`` seconds (default 5 minutes) of an
    SRT, WebVTT or ASS/SSA subtitle - or the plain text itself when it carries
    no subtitle timestamps (such as an already-windowed audio transcript)."""
    raw = (text or "").lstrip("\ufeff")
    if not raw.strip():
        return ""
    if _SRT_TIME.search(raw):
        return opening_text(raw, limit=limit)
    ass_lines: list[str] = []
    saw_ass = False
    for line in raw.splitlines():
        match = _ASS_DIALOGUE.match(line.strip())
        if not match:
            continue
        saw_ass = True
        start = (int(match.group(1)) * 3600
                 + int(match.group(2)) * 60
                 + int(match.group(3)))
        if start >= limit:
            continue
        spoken = _ASS_TAGS.sub("", match.group(4))
        spoken = spoken.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
        spoken = " ".join(spoken.split())
        if spoken:
            ass_lines.append(spoken)
    if saw_ass:
        return "\n".join(ass_lines)
    return "\n".join(line.strip() for line in raw.splitlines() if line.strip())


def intro_mentions(srt: str) -> Counter:
    """The words that follow somebody saying who is there, in the opening
    minutes of a transcript, counted.

    Read from the cues that start inside :data:`INTRO_SECONDS`, and only
    within a few words of an opening: a director talks about the actors all
    the way through, and the names that say who is IN the commentary are the
    ones it opens by giving.
    """
    words = _words(opening_text(srt))
    # A word is counted once however many openings reach it: "Hi, I'm" is
    # two of them in front of one name.
    reached: set[int] = set()
    for position in range(len(words)):
        for phrase in _INTRO_PHRASES:
            if tuple(words[position:position + len(phrase)]) != phrase:
                continue
            after = position + len(phrase)
            reached.update(range(after, min(after + _INTRO_REACH, len(words))))
            break
    return Counter(words[position] for position in reached)


# --- what the tracks are to be called ---------------------------------------

_LEADING_AUDIO = re.compile(r"^\s*audio\s+(commentar)", re.IGNORECASE)

# A bracketed note at the very end of a listing, brackets inside it allowed one
# level deep.
_TRAILING_NOTE = re.compile(r"\s*[(\[]((?:[^()\[\]]|\([^()]*\))*)[)\]]$")


def track_title(description: str) -> str:
    """The name a listed commentary gives its track: the listing's own words,
    with a leading "Audio commentary" shortened to "Commentary" and anything
    that is not a commentary's name trimmed off the ends.

    A name that no longer says it is a commentary is given the word in front,
    so the track still reads as one to everything that asks it by its name.
    """
    title = " ".join((description or "").split()).strip(" .;:-")
    # A note in brackets at the end that names nobody - "(38:43)", "(4
    # alternate endings)", "(new)" - is about the disc, not the commentary.
    while True:
        note = _TRAILING_NOTE.search(title)
        if not note or participants(note.group(1)):
            break
        title = title[:note.start()].rstrip(" .;:-")
    title = _LEADING_AUDIO.sub(lambda m: m.group(1).capitalize(), title)
    if title[:1].islower():
        title = title[:1].upper() + title[1:]
    if not languages.is_commentary_name(title):
        title = "Commentary: " + title
    return title


# --- the decision -------------------------------------------------------------

def _candidate_lists(releases: list, disc_kind: str, count: int) -> list:
    """Every set of ``count`` commentaries a disc of this kind carries, one
    entry per different set, each with the releases that carry it.

    A set and not a list: the order a release lists its commentaries in is its
    menu's, which says nothing about the order of the disc's audio streams, so
    two releases that list the same commentaries in another order agree. And
    two listings of one commentary are the same commentary when they name the
    same people - releases word their extras each in their own way, and "Audio
    commentary with director X" is "Commentary by X" said by another hand.
    """
    found: list = []
    for release in releases:
        seen_here = set()
        for disc in release.discs:
            if disc.format != disc_kind or len(disc.commentaries) != count:
                continue
            key = tuple(sorted(_same_key(entry) for entry in disc.commentaries))
            if key in seen_here:
                continue
            seen_here.add(key)
            for entry in found:
                if entry[0] == key:
                    entry[2].append(release.label)
                    break
            else:
                found.append((key, disc.commentaries, [release.label]))
    return found


def _same_key(description: str) -> str:
    """What two listings of one commentary have in common: the people in it,
    or, for one that names nobody, its words."""
    names = participants(description)
    if names:
        return "names: " + " ".join(sorted(names))
    return "words: " + " ".join(_words(description))


def _score(names: frozenset, mentions: Counter) -> int:
    """How strongly a transcript's opening introduces these people. Each name
    counts at most three times, so one name said over and over cannot outweigh
    two names said once each."""
    return sum(min(mentions.get(name, 0), 3) for name in names)


def _fits(order: tuple, alone: list, scores: list, left_over: int) -> bool:
    """Whether laying the i-th listed commentary on track ``order[i]`` is
    proved.

    Every commentary whose people ARE introduced somewhere must be introduced
    on its own track more strongly than on any other, and its track must
    introduce nobody else's people as strongly. One commentary may be placed
    without that - one that names nobody of its own, or whose people never say
    who they are - and it takes the track left over, which must then introduce
    nobody listed for another. ``left_over`` is how many may be placed that
    way: one, or none at all when the releases disagree about what the
    commentaries are, because a list placed by elimination fits a track that
    is some other release's commentary just as well.
    """
    size = len(order)
    for i in range(size):
        track = order[i]
        if not alone[i] or not any(scores[i]):
            left_over -= 1
            if any(scores[j][track] for j in range(size) if j != i):
                return False
            continue
        own = scores[i][track]
        rivals = [scores[i][t] for t in range(size) if t != track] \
            + [scores[j][track] for j in range(size) if j != i]
        if own == 0 or (rivals and max(rivals) >= own):
            return False
    return left_over >= 0


def _proved_order(listing: tuple, transcripts: list, title: str,
                  left_over: int = 1) -> tuple:
    """(which track each listed commentary is, "") when the transcripts prove
    it - the i-th listed commentary on the ``order[i]``-th track - or (None,
    why not).

    Only the names that belong to one commentary alone tell them apart - a
    director on two of them is on both. Every way of laying the list on the
    tracks is tried, and exactly one may fit (:func:`_fits`, which
    ``left_over`` is handed on to).
    """
    if any(transcript is None for transcript in transcripts):
        return None, ("there is no transcript of every commentary to tell "
                      "them apart by")
    people = [participants(entry, title) for entry in listing]
    alone = [names - frozenset().union(*(people[:i] + people[i + 1:]))
             for i, names in enumerate(people)]
    unnamed = sum(1 for names in alone if not names)
    if unnamed > left_over:
        return None, "%d of the listed commentaries name nobody of their own" \
            % unnamed
    mentions = [intro_mentions(transcript) for transcript in transcripts]
    size = len(listing)
    scores = [[_score(alone[i], mentions[t]) for t in range(size)]
              for i in range(size)]
    fitting = [order for order in permutations(range(size))
               if _fits(order, alone, scores, left_over)]
    if len(fitting) == 1:
        return fitting[0], ""
    if not fitting:
        return None, ("the commentaries' opening minutes do not say which "
                      "track is which")
    return None, "the transcripts fit more than one order"


def _valid_order(order: object, count: int) -> tuple[int, ...] | None:
    """``order`` as a tuple when it is one of the permutations
    :func:`_proved_order` tries on ``count`` tracks, else None."""
    if not isinstance(order, (tuple, list)) or len(order) != count:
        return None
    if not all(type(item) is int for item in order):
        return None
    if sorted(order) != list(range(count)):
        return None
    return tuple(order)


def decide(file_tracks: list, releases: list | None, disc_kind: str,
           transcripts: dict, title: str = "", *,
           fallback: Callable | None = None) -> Decision:
    """The names to give one film's commentary tracks, or why none are given.

    ``file_tracks`` are the film's commentary audio tracks as
    :class:`FileCommentary`, in the file's order. ``releases`` is what the
    database lists for the film, None when it could not say which film this
    is. ``disc_kind`` is :func:`disc_format`'s answer for the file.
    ``transcripts`` maps a track id to the text of its transcript, where one
    is to hand. ``title`` is the film's, whose words are nobody's name.

    When the deterministic rules cannot settle which candidate listing or
    which track order fits - and every track has a transcript - ``fallback``,
    if given, is called with ``(ordered, candidates, texts, title)`` where
    ``candidates`` is ``[(commentaries, tuple(release_labels)), ...]``, and
    may return ``(listing_index, order, note)``. ``listing_index`` must name
    one of ``candidates`` and ``order`` must be one of the permutations
    :func:`_proved_order` tried; any other answer is refused and the existing
    reason stands.
    """
    ordered, why = order_file_commentaries(file_tracks)
    if not ordered:
        return Decision((), why)
    if not disc_kind:
        return Decision((), "the file's picture size could not be read")
    if releases is None:
        return Decision((), "the database has no one page of this film on %s"
                        % _KIND_NAMES[disc_kind])
    count = len(ordered)
    lists = _candidate_lists(releases, disc_kind, count)
    if not lists:
        listed = sorted({len(disc.commentaries) for release in releases
                         for disc in release.discs
                         if disc.format == disc_kind})
        return Decision((), "the file has %d commentar%s, and no %s release "
                        "lists that many (%s)"
                        % (count, "y" if count == 1 else "ies",
                           _KIND_NAMES[disc_kind],
                           "they list " + ", ".join(str(n) for n in listed)
                           if listed else "there is none"))

    texts = [transcripts.get(track.id) for track in ordered]
    model_note = ""
    order: tuple[int, ...]
    if count == 1 and len(lists) == 1:
        # One commentary has no order to get wrong, and every release of this
        # kind agrees on what it is.
        chosen, order = lists[0], (0,)
    else:
        proved = []
        reasons = []
        for entry in lists:
            fit, why = _proved_order(entry[1], texts, title,
                                     1 if len(lists) == 1 else 0)
            if fit is None:
                reasons.append(why)
            else:
                proved.append((entry, fit))
        if len(proved) == 1:
            chosen, order = proved[0]
        else:
            refusal = (reasons[0] if len(lists) == 1 else
                       "%d %s releases list different commentaries, "
                       "and the transcripts %s"
                       % (len(lists), _KIND_NAMES[disc_kind],
                          "fit none of them" if not proved
                          else "fit more than one"))
            settled = None
            if fallback is not None and all(t is not None for t in texts):
                candidates = [(entry[1], tuple(entry[2])) for entry in lists]
                index, picked_order, note = fallback(
                    ordered, candidates, texts, title)
                model_note = note or ""
                valid = _valid_order(picked_order, count)
                if (type(index) is int and 0 <= index < len(lists)
                        and valid is not None):
                    settled = (lists[index], valid)
            if settled is None:
                return Decision((), refusal, model_note=model_note)
            chosen, order = settled

    given = {ordered[order[i]].id: track_title(chosen[1][i])
             for i in range(count)}
    names = tuple((track.id, given[track.id]) for track in ordered)
    return Decision(names, "", ", ".join(chosen[2]), model_note=model_note)


_KIND_NAMES = {"uhd": "4K Blu-ray", "bluray": "Blu-ray", "dvd": "DVD"}


# --- what else goes by a commentary's name -------------------------------------

# What an append puts on the end of a transcript subtitle's title when one
# commentary has transcripts in more than one language: " (EN)".
_LANGUAGE_MARKER = re.compile(r"\s*\([A-Za-z]{2,3}\)$")


def subtitle_renames(tracks: list, renamed: list) -> tuple[list, str]:
    """The film's commentary subtitles to rename along with their audio, as
    (track id, new name) pairs - or ([], why not).

    ``tracks`` are the film's tracks as the ingest reads them, ``renamed``
    pairs each commentary's OLD audio name with its new one. Two kinds of
    subtitle go by a commentary's name: the transcript the ingest appended,
    under the audio's own name with a language marker when there are several,
    and the disc's own subtitles for it, which a rip numbers the way it numbers
    the audio ("English (Commentary #2)"). The first is matched by the name,
    the second by the number, and both keep their marker.

    A subtitle that is only numbered and matches no renamed commentary - or
    matches a name two commentaries share - cannot be told which commentary it
    is of. Left numbered while its audio is renamed, it would no longer point
    at anything, so the film is left alone instead. A subtitle somebody named
    ("German for the director's commentary") is left as it is.
    """
    targets = dict(renamed)
    olds = [old for old, _new in renamed]
    by_number = {generic_number(old): new for old, new in renamed}
    out = []
    for track in tracks:
        if not (track.is_subtitle and track.is_commentary):
            continue
        marker = _LANGUAGE_MARKER.search(track.name)
        base = track.name[:marker.start()] if marker else track.name
        number = generic_number(base)
        if base in targets:
            if olds.count(base) > 1:
                return [], ('the commentary subtitle "%s" could be of more '
                            "than one commentary" % track.name)
            target = targets[base]
        elif number is not None:
            if number not in by_number or len(by_number) != len(renamed):
                return [], ('the commentary subtitle "%s" cannot be told which '
                            "commentary it is of" % track.name)
            target = by_number[number]
        else:
            continue
        out.append((track.id, target + (marker.group(0) if marker else "")))
    return out, ""


_SIDECAR_ROLE_TAGS = frozenset((
    "forced", "sdh", "cc", "hi", "default", "commentary",
))


def _strip_sidecar_lang_tags(named: str) -> tuple[str, str]:
    """Split trailing dot-separated language/role tags off a sidecar's label
    portion (e.g. ``'Commentary 1.en'`` -> ``('Commentary 1', '.en')``)."""
    parts = named.split(".")
    tail: list[str] = []
    while len(parts) > 1:
        token = parts[-1].strip().lower()
        if (languages.code_from_tag(token)
                or token in _SIDECAR_ROLE_TAGS - {"commentary"}):
            tail.append(parts.pop())
        else:
            break
    suffix = "." + ".".join(reversed(tail)) if tail else ""
    return ".".join(parts), suffix


def _parse_sidecar_commentary(name: str, movie_stem: str) -> tuple[str, str, str, str] | None:
    """Parse a sidecar beside ``movie_stem`` that belongs to a commentary.

    Returns ``(kind, track_id, label, suffix)`` or ``None``:
    - ``kind == "indexed"``: ``"<stem> <track_id> <label><suffix>"`` (ingest
      commentary extract/transcript, ``.opus`` or text subtitle);
    - ``kind == "plain"``: ``"<stem><sep><label><suffix>"`` (ripped or external
      commentary text subtitle without a track id, where ``sep`` is ``"."`` or
      ``" "`` and ``label`` is generically named).
    """
    if not movie_stem or not name.startswith(movie_stem):
        return None
    rest = name[len(movie_stem):]
    if not rest:
        return None
    indexed = re.match(r"^ ([0-9]+) (\S.*)$", rest, re.S)
    if indexed:
        track_id, tail = indexed.group(1), indexed.group(2)
        named, suffix = plexnames._split_suffix(tail)
        ext = suffix.rsplit(".", 1)[-1].lower() if suffix else ""
        if ext in plexnames.COMMENTARY_EXTENSIONS or ext in TEXT_SUBTITLE_EXTENSIONS:
            if named:
                return ("indexed", track_id, named, suffix)
        return None
    sep = rest[0]
    if sep not in (".", " "):
        return None
    named, suffix = plexnames._split_suffix(rest[1:])
    ext = suffix.rsplit(".", 1)[-1].lower() if suffix else ""
    if ext not in TEXT_SUBTITLE_EXTENSIONS or not named:
        return None
    base_label, extra_tags = _strip_sidecar_lang_tags(named)
    marker = _LANGUAGE_MARKER.search(base_label)
    clean_label = base_label[:marker.start()] if marker else base_label
    if generic_number(clean_label) is None:
        return None
    return ("plain", sep, base_label, extra_tags + suffix)


def generic_groups(tracks: list, sidecar_names: Sequence[str] = (),
                   movie_stem: str = "") -> list[CommentaryGroup]:
    """Group every generically named commentary surface of a film - audio
    tracks, embedded commentary subtitles (including multiple translations) and
    sidecar commentary files (including multiple translations) - by the
    commentary track they represent.

    Each returned :class:`CommentaryGroup` can be analysed once and its
    resolved name applied to every audio track, embedded subtitle and sidecar
    in the group.
    """
    generic_audio = [
        track for track in tracks
        if track.is_audio and track.is_commentary
        and generic_number(track.name) is not None
    ]
    unnumbered_audio_count = sum(
        1 for track in generic_audio if generic_number(track.name) == 0)

    buckets: dict[str, dict] = {}
    order: list[str] = []
    audio_id_to_key: dict[str, str] = {}

    def ensure(key: str, number: int, label: str) -> dict:
        if key not in buckets:
            buckets[key] = {
                "number": number,
                "label": label,
                "audio_ids": [],
                "subtitle_ids": [],
                "sidecars": [],
            }
            order.append(key)
        return buckets[key]

    for track in generic_audio:
        num = generic_number(track.name) or 0
        if num > 0:
            key = "num:%d" % num
        elif unnumbered_audio_count <= 1:
            key = "num:0"
        else:
            key = "track:%s" % track.id
        entry = ensure(key, num, track.name)
        entry["audio_ids"].append(track.id)
        audio_id_to_key[track.id] = key

    for track in tracks:
        if not (track.is_subtitle and track.is_commentary):
            continue
        marker = _LANGUAGE_MARKER.search(track.name)
        base = track.name[:marker.start()] if marker else track.name
        sub_num = generic_number(base)
        if sub_num is None:
            continue
        if sub_num == 0 and unnumbered_audio_count > 1:
            continue
        key = "num:%d" % sub_num
        entry = ensure(key, sub_num, base)
        entry["subtitle_ids"].append(track.id)

    for filename in sorted(sidecar_names):
        parsed = _parse_sidecar_commentary(filename, movie_stem)
        if parsed is None:
            continue
        kind, token, label, _suffix = parsed
        if kind == "indexed":
            if token in audio_id_to_key:
                key = audio_id_to_key[token]
                buckets[key]["sidecars"].append(filename)
                continue
            sc_num = generic_number(label)
            if sc_num is None or (sc_num == 0 and unnumbered_audio_count > 1):
                continue
            key = "num:%d" % sc_num
            entry = ensure(key, sc_num, label)
            entry["sidecars"].append(filename)
        else:
            marker = _LANGUAGE_MARKER.search(label)
            base = label[:marker.start()] if marker else label
            plain_num = generic_number(base)
            if plain_num is None or (plain_num == 0 and unnumbered_audio_count > 1):
                continue
            key = "num:%d" % plain_num
            entry = ensure(key, plain_num, base)
            entry["sidecars"].append(filename)

    return [
        CommentaryGroup(
            key=key,
            number=buckets[key]["number"],
            label=buckets[key]["label"],
            audio_ids=tuple(buckets[key]["audio_ids"]),
            subtitle_ids=tuple(buckets[key]["subtitle_ids"]),
            sidecars=tuple(buckets[key]["sidecars"]),
        )
        for key in order
    ]


def group_sidecar_renames(sidecars: Sequence[str], movie_stem: str,
                          new_name: str, clean: Callable[[str], str],
                          old_audio_names: dict[str, str] | None = None
                          ) -> list[tuple[str, str]]:
    """Rename every sidecar in one :class:`CommentaryGroup` (both indexed
    ``<stem> <id> <name>.xx.srt`` transcripts and plain
    ``<stem>.Commentary.xx.srt`` sidecars) to ``new_name``, preserving track
    ids, language markers/extensions and filename byte-length limits."""
    out: list[tuple[str, str]] = []
    new_clean = clean(new_name)
    old_audio_names = old_audio_names or {}
    for filename in sorted(sidecars):
        parsed = _parse_sidecar_commentary(filename, movie_stem)
        if parsed is None:
            continue
        kind, token, label, suffix = parsed
        if kind == "indexed":
            if token in old_audio_names:
                old_clean = clean(old_audio_names[token])
                if not old_clean.startswith(label):
                    continue
            stem = commentarytranscription.commentary_stem(
                movie_stem, token, new_clean)
            while stem and not plexnames.fits_name(stem + suffix):
                stem = stem[:-1]
            if stem and stem + suffix != filename:
                out.append((filename, stem + suffix))
        else:
            marker = _LANGUAGE_MARKER.search(label)
            suffix_with_marker = (marker.group(0) if marker else "") + suffix
            candidate = movie_stem + token + new_clean
            while candidate and not plexnames.fits_name(
                    candidate + suffix_with_marker):
                candidate = candidate[:-1]
            if candidate and candidate + suffix_with_marker != filename:
                out.append((filename, candidate + suffix_with_marker))
    return out


def sidecar_renames(names: list, movie_stem: str, tracks: list,
                    clean) -> list:
    """The sidecars beside the film to rename along with their commentary's
    audio, as (old name, new name) pairs.

    A commentary's transcripts and extract are named "<film> <track id>
    <track name>", the name cleaned the way the transcription cleans it
    (``clean``) and cut to fit, and then their suffixes. ``tracks`` are
    (track id, old name, new name) triples. Plain sidecar commentary subtitles
    (such as ``"<film>.Commentary 1.en.srt"``) matching a renamed commentary's
    generic number are renamed alongside them. Only a sidecar whose name is
    the old one - or the old one cut short - is renamed, and it is cut the way
    the transcription would have cut the new one, so the next run finds it
    where it looks.
    """
    out = []
    seen_old: set[str] = set()
    for track_id, old, new in tracks:
        prefix = commentarytranscription.commentary_prefix(movie_stem,
                                                           track_id)
        old_clean = clean(old)
        new_clean = clean(new)
        for name in sorted(names):
            if not name.startswith(prefix):
                continue
            named, suffix = plexnames._split_suffix(name[len(prefix):])
            ext = suffix.rsplit(".", 1)[-1].lower() if suffix else ""
            if ext not in plexnames.COMMENTARY_EXTENSIONS \
                    and ext not in TEXT_SUBTITLE_EXTENSIONS:
                continue
            if not named or not old_clean.startswith(named):
                continue
            stem = commentarytranscription.commentary_stem(movie_stem,
                                                           track_id, new_clean)
            while stem and not plexnames.fits_name(stem + suffix):
                stem = stem[:-1]
            if stem and stem + suffix != name:
                out.append((name, stem + suffix))
                seen_old.add(name)

    by_number = {generic_number(old): new for _id, old, new in tracks
                 if generic_number(old) is not None}
    if len(by_number) == len(tracks):
        for name in sorted(names):
            if name in seen_old:
                continue
            parsed = _parse_sidecar_commentary(name, movie_stem)
            if parsed is None or parsed[0] != "plain":
                continue
            _kind, sep, label, suffix = parsed
            marker = _LANGUAGE_MARKER.search(label)
            base = label[:marker.start()] if marker else label
            num = generic_number(base)
            if num not in by_number:
                continue
            new_clean = clean(by_number[num])
            suffix_with_marker = (marker.group(0) if marker else "") + suffix
            candidate = movie_stem + sep + new_clean
            while candidate and not plexnames.fits_name(
                    candidate + suffix_with_marker):
                candidate = candidate[:-1]
            if candidate and candidate + suffix_with_marker != name:
                out.append((name, candidate + suffix_with_marker))
                seen_old.add(name)
    return out
