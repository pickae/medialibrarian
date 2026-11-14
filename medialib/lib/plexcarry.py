"""What of a Plex library's copy of a film is carried into the copy being
ingested, and what is not and why.

The comparison (:mod:`medialib.lib.plexcompare`) says what each file has that
the other lacks. This turns what the library's copy has into what the new copy
can take from it:

- its commentaries' NAMES, where the new copy's are only numbered and the two
  have as many commentaries in the same languages - a header edit, which needs
  the two in sync no more than a track name does;
- its named CHAPTERS, where the new copy's are numbered or missing - their
  times carried over the measured timing, so the two must be one cut;
- the audio the new copy LACKS: a soundtrack in a language the improved copy
  keeps, the commentaries (with their transcripts), an isolated score or an
  audio description. Muxed across at the measured offset; a copy that runs at
  another speed is only bridged where the track is being encoded anyway - a
  lossless one, on its way to opus - and a lossy one is left where it is.

Never the picture, never the subtitles, and never a soundtrack swapped for a
better one in a language the new copy already has: those stay the verdict's
to say, for a person to settle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from medialib.lib import chapterdb, commentarynames, languages
from medialib.lib.plexcompare import (
    Audio,
    Profile,
    Timing,
    _best_per_language,
    _other_kind,
    _sound,
    editions_differ,
)

__all__ = ["Carried", "Plan", "plan", "NOTHING"]


class Carried(NamedTuple):
    """One audio track of the library's copy, carried into the new one.

    ``kind`` is "soundtrack", "commentary" or "other"; ``encode`` that it is
    lossless and goes to opus on its way in, ``stretch`` that it is put to
    the new copy's speed while it does. ``transcripts`` are the commentary's
    transcripts beside the library's copy, as ``(srt, two-letter language)``
    - none for a commentary whisper is to transcribe."""

    audio: Audio
    kind: str
    encode: bool
    stretch: bool
    transcripts: tuple = ()


class Plan(NamedTuple):
    """What is taken from the library's copy at ``library``: the ``audio``
    tracks (:class:`Carried`), the ``names`` the new copy's numbered
    commentaries are given, as ``(their track id, name)``, and the ``chapters``
    in the new copy's time. ``carried`` and ``left`` are said in words: what
    is taken, and what the library's copy has that is not, with why."""

    library: str
    timing: Timing | None
    audio: tuple
    names: tuple
    chapters: chapterdb.ChapterSet | None
    carried: tuple
    left: tuple

    @property
    def anything(self) -> bool:
        return bool(self.audio or self.names or self.chapters)


NOTHING = Plan("", None, (), (), None, (), ())

# What a soundtrack's language is judged by: the improved copy keeps it.
Keeps = Callable[[Audio], bool]
# The transcripts of a commentary of the library's copy, beside it.
Transcripts = Callable[[Audio], tuple]


def _language(audio: Audio) -> str:
    return languages.track_language(audio.language, audio.ietf) \
        or audio.language


def _said(audio: Audio) -> str:
    return '"%s"' % audio.name if audio.name else "(%s)" % audio.language


def plan(new: Profile, old: Profile, timing: Timing | None, untimed: str,
         keeps: Keeps, transcripts: Transcripts,
         old_chapters: chapterdb.Existing | None = None) -> Plan:
    """What the new copy takes from ``old``. ``timing`` is the measured line
    between the two, None with ``untimed`` saying why there is none; ``keeps``
    says whether the improved copy keeps a soundtrack's language;
    ``transcripts`` finds a commentary's beside the library's copy; and
    ``old_chapters`` are its chapters as :func:`chapterdb.existing_chapters`
    reads them."""
    edition = editions_differ(new, old)
    if edition:
        return NOTHING._replace(library=old.path, left=(
            "nothing: a different edition (%s), kept beside it" % edition,))
    audio: list = []
    carried: list = []
    left: list = []

    def needs_sync(what: str) -> bool:
        if timing is None:
            left.append("%s: the two cannot be put in sync (%s)"
                        % (what, untimed))
            return False
        return True

    def take(track: Audio, kind: str, what: str, found=()) -> None:
        if timing is None:
            needs_sync(what)
            return
        steady = timing.steady(new.seconds)
        if not steady and not track.lossless:
            left.append("%s: the existing file runs at another speed, and a "
                        "lossy track cannot be put to the new one's without "
                        "encoding it again" % what)
            return
        audio.append(Carried(track, kind, track.lossless, not steady, found))
        carried.append(what + (" (to opus%s)" % (
            "" if steady else ", put to the new file's speed")
            if track.lossless else ""))

    names = _names(new, old)
    for track_id, name in names:
        carried.append('commentary name: track %d "%s" -> "%s"' % (
            track_id, next(mine.name for mine in new.commentaries
                           if mine.track == track_id), name))

    for track in _commentaries_to_carry(new, old, left):
        found = transcripts(track)
        take(track, "commentary", "the commentary %s%s" % (
            _said(track), ", with its transcripts" if found
            else ", whisper transcribing it"), found)

    mine = {_language(track) for track in new.audio}
    for language, (_score, track) in sorted(_best_per_language(old.audio).items()):
        if _language(track) in mine:
            theirs = _best_per_language(new.audio).get(language)
            if theirs is not None and _rank(track) > _rank(theirs[1]):
                left.append("the better %s soundtrack (%s against %s): a "
                            "soundtrack is never swapped for another, that is "
                            "yours to do" % (language, _sound(track),
                                             _sound(theirs[1])))
            continue
        what = "the %s soundtrack (%s)" % (track.language, _sound(track))
        if not keeps(track):
            left.append("%s: not a language the improved copy keeps" % what)
            continue
        take(track, "soundtrack", what)

    new_kinds = {_other_kind(track.name) or "audio description"
                 for track in new.others}
    for track in old.others:
        kind = _other_kind(track.name) or "audio description"
        if kind in new_kinds:
            continue
        what = "the %s track %s" % (kind, _said(track))
        if not keeps(track):
            left.append("%s: not a language the improved copy keeps" % what)
            continue
        take(track, "other", what)

    chapters = None
    if (old_chapters is not None and old_chapters.kind == "named"
            and not (new.chapters and new.named_chapters)
            and needs_sync("the %d named chapters" % len(old_chapters.names))
            and timing is not None):
        chapters = _chapters_in_new_time(old_chapters, timing, new.seconds)
        carried.append("the %d named chapters, to the new file's time"
                       % len(chapters.chapters))

    return Plan(old.path, timing, tuple(audio), tuple(names), chapters,
                tuple(carried), tuple(left))


def _rank(track: Audio) -> tuple:
    return (track.channels, track.objects, track.lossless)


def _numbered(track: Audio) -> bool:
    return commentarynames.generic_number(track.name) is not None


def _names(new: Profile, old: Profile) -> list:
    """The names the new copy's numbered commentaries take from the
    library's: one for one, in order, where every one of the new copy's is
    only numbered, every one of the library's named, and the two have as
    many in the same languages."""
    if not new.commentaries or len(new.commentaries) != len(old.commentaries):
        return []
    pairs = list(zip(new.commentaries, old.commentaries, strict=True))
    if not all(_numbered(mine) and not _numbered(theirs)
               and _language(mine) == _language(theirs)
               for mine, theirs in pairs):
        return []
    return [(mine.track, theirs.name) for mine, theirs in pairs]


def _commentaries_to_carry(new: Profile, old: Profile, left: list) -> list:
    """The library's commentaries the new copy lacks: every one of them where
    it has none, and where it has some, the ones whose names none of its own
    have - which needs its own to be named rather than numbered."""
    if not new.commentaries:
        return list(old.commentaries)
    if len(old.commentaries) <= len(new.commentaries):
        return []
    if any(_numbered(track) for track in new.commentaries):
        left.append("%d commentaries against the new file's %d, which are only "
                    "numbered: which of them it lacks cannot be told"
                    % (len(old.commentaries), len(new.commentaries)))
        return []
    have = {track.name.casefold().strip() for track in new.commentaries}
    return [track for track in old.commentaries
            if track.name.casefold().strip() not in have]


def _chapters_in_new_time(existing: chapterdb.Existing, timing: Timing,
                          seconds: float) -> chapterdb.ChapterSet:
    """The library's chapters with their starts carried over ``timing``: the
    first one starting the film whatever is in front of it now, and any the
    new copy ends before dropped."""
    chapters: list[tuple[float, str]] = []
    for start, name in zip(existing.starts, existing.names, strict=True):
        moved = 0.0 if not chapters else round(timing.to_new(start), 3)
        if seconds and moved >= seconds:
            break
        if chapters and moved <= chapters[-1][0]:
            continue
        chapters.append((max(0.0, moved), name))
    return chapterdb.ChapterSet("", "", "the library's copy", "", 0, seconds,
                                tuple(chapters))
