"""A film being ingested held up against the copy a Plex library already has.

What is compared is what decides which file to keep, or what to carry from one
into the other: the picture (resolution tier, HDR), the soundtracks per
language, the commentaries and other bonus audio, and the chapters. Subtitles
are left out on purpose - they are fetched and synced per file anyway.

Before any of that, the two have to be the same cut. Their edition tags differ
and they are two versions of the film rather than two copies of one. Otherwise
the audio decides: :func:`measure_offset` cross-correlates the two soundtracks
at several points of the film, and where the offsets found lie on one straight
line (:meth:`Offset.timing`) the two are one cut - a logo more or less in front
is the line's start, and a copy running at another speed (24 against 23.976
frames a second, or PAL's 25) is its slope. Only without that measurement do
the lengths decide, a minute or more apart being two versions.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import NamedTuple

from medialib.lib import chapterdb, filmrecord, languages

__all__ = ["Audio", "Profile", "Comparison", "Offset", "Timing",
           "read_profile", "compare", "measure_offset", "audio_pair", "tier"]

# Two lengths this far apart are two cuts of the film, not one cut with a
# different logo in front of it.
VERSION_GAP = 60.0

# A PAL transfer runs 25/23.976 times faster than the film: the same cut,
# about 4% shorter, whose audio cannot be muxed across without restretching.
PAL_SPEEDUP = 25 / (24000 / 1001)

# Below this the lengths are the same, and no offset is worth looking for
# unless the measurement finds one anyway.
SAME_LENGTH = 0.5

# An offset the measurement finds smaller than this is no offset at all: the
# codecs' own priming delay is in this range.
NEGLIGIBLE_MS = 40

# How far apart the offsets measured at different points may be and still be
# one offset - beyond it, something was cut or added inside the film. The same
# bound holds for how far any of them may stray from the straight line the
# others make with it.
CONSISTENT_MS = 40

# How far two copies may drift apart over the whole film and still count as
# running at one speed: below it a track is muxed across with an offset alone.
STEADY_DRIFT = 0.02

# The audio cross-correlated: mono, at a rate that keeps a minute of it small
# and still places a match to an eighth of a millisecond.
_RATE = 8000
_WINDOW = 20.0
# Five points rather than three: two of them fix the line, and the other three
# are what says it is one.
_POINTS = (0.1, 0.3, 0.5, 0.7, 0.9)
# How far either side of where the new window starts the old one is searched,
# beyond the length difference itself.
_REACH = 30.0
# The envelope's step, in samples: a hundredth of a second.
_HOP = _RATE // 100
# How far either side of the envelope's match the waveform is searched.
_REFINE = 2 * _HOP
# Below these normalised correlations the peak is noise, not a match.
_TRUSTED_ENVELOPE = 0.5
_TRUSTED_WAVEFORM = 0.3

# Bonus audio that is neither a commentary nor a soundtrack to watch the film in.
_OTHER_AUDIO = (("isolated score", re.compile(r"isolat|score only|music only",
                                              re.IGNORECASE)),
                ("music & effects", re.compile(r"effects|\bm ?& ?e\b", re.IGNORECASE)),
                ("audio description", re.compile(r"descripti", re.IGNORECASE)),
                ("interview", re.compile(r"interview", re.IGNORECASE)))

# The HDR kinds, worst to best.
_HDR_RANK = {"SDR": 0, "HLG": 1, "HDR10": 1, "HDR10+": 2, "Dolby Vision": 3}


class Audio(NamedTuple):
    """One audio track: its 0-based index among the file's audio streams
    (what ffmpeg's ``-map 0:a:N`` takes), and what it is. ``track`` is its id
    in the file (what mkvmerge's ``-a`` takes), ``ietf`` its BCP 47 tag and
    ``rate`` its sample rate - all three what carrying it into another file
    needs."""

    stream: int
    language: str
    channels: int
    lossless: bool
    objects: bool
    name: str
    track: int = -1
    ietf: str = ""
    rate: int = 0


class Profile(NamedTuple):
    path: str
    seconds: float
    width: int
    height: int
    hdr: str
    audio: tuple        # the soundtracks
    commentaries: tuple  # Audio
    others: tuple        # Audio: isolated scores, descriptive audio
    chapters: int
    named_chapters: bool
    edition: str


class Comparison(NamedTuple):
    """``verdict`` is "different", "same", "new", "old" or "mixed"; the two
    lists say what each file has the other lacks, and ``versions`` why the
    two are not one cut."""

    verdict: str
    new_better: list
    old_better: list
    versions: list


class Offset(NamedTuple):
    """The delay to give the old file's tracks when muxing them into the new
    one, in milliseconds - one per point measured, None where nothing
    matched."""

    at: tuple
    ms: tuple

    @property
    def measured(self) -> list:
        return [ms for ms in self.ms if ms is not None]

    @property
    def consistent(self) -> bool:
        found = self.measured
        return len(found) >= 2 and max(found) - min(found) <= CONSISTENT_MS

    @property
    def ms_median(self) -> int:
        found = sorted(self.measured)
        return found[len(found) // 2]

    def timing(self) -> Timing | None:
        """Where the old file's moments fall in the new one, when the offsets
        measured lie on one straight line - None when fewer than three points
        matched, or one of them strays from the line: then the film was cut
        differently somewhere inside it."""
        points = [(at, ms) for at, ms in zip(self.at, self.ms, strict=True)
                  if ms is not None]
        if len(points) < 3:
            return None
        count = len(points)
        mean_at = sum(at for at, _ms in points) / count
        mean_ms = sum(ms for _at, ms in points) / count
        spread = sum((at - mean_at) ** 2 for at, _ms in points)
        if not spread:
            return None
        slope = sum((at - mean_at) * (ms - mean_ms)
                    for at, ms in points) / spread
        start = mean_ms - slope * mean_at
        if any(abs(ms - (start + slope * at)) > CONSISTENT_MS
               for at, ms in points):
            return None
        # The offset at a moment of the new file is new - old there, so
        # old = new * (1 - k) - a, with k and a in seconds per second and
        # seconds; turned round, new = (old + a) / (1 - k).
        drift = slope / 1000.0
        speed = 1.0 / (1.0 - drift)
        return Timing(start / 1000.0 * speed, speed)


class Timing(NamedTuple):
    """Where a moment of the old file falls in the new one: ``start`` plus
    ``speed`` times it, in seconds - the logo more or less in front, and the
    other speed the copy runs at."""

    start: float
    speed: float

    def to_new(self, seconds: float) -> float:
        return self.start + self.speed * seconds

    def steady(self, seconds: float) -> bool:
        """Whether the two run at one speed over a film this long: what an
        offset alone puts in sync."""
        return abs(self.speed - 1.0) * seconds < STEADY_DRIFT

    def describe(self, seconds: float) -> str:
        """The timing in words, for a film this long."""
        said = "%+d ms" % round(self.start * 1000)
        if self.steady(seconds):
            return "the same cut, the existing file's tracks %s off" % said
        rate = _rates(self.speed)
        return ("the same cut at another speed: the existing file %s %s, "
                "and %s off at its start" % (
                    "runs faster than the new one" if self.speed > 1
                    else "runs slower than the new one",
                    rate or "by %.2f%%" % (abs(self.speed - 1) * 100), said))


# The speeds a film is sold at, and the ratios between them that a copy running
# at one against the other shows.
_FILM_RATES = (("24", 24.0), ("23.976", 24000 / 1001), ("25", 25.0))


def _rates(speed: float) -> str:
    """The two frame rates a speed ratio - the old file's rate over the new
    one's - is, as "(24 fps against the new file's 23.976)", or "" for one
    that is not two of the rates films are sold at."""
    for name, rate in _FILM_RATES:
        for other_name, other in _FILM_RATES:
            if rate != other and abs(rate / other - speed) < 0.0002:
                return "(%s fps against the new file's %s)" % (name, other_name)
    return ""


def _run_json(argv: list) -> dict:
    try:
        done = filmrecord.output(argv, argv[-1])
        document = json.loads(done.stdout.decode("utf-8", "surrogateescape"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _number(value, kind=int):
    try:
        return kind(value)
    except (TypeError, ValueError):
        return kind(0)


def _hdr(video: dict) -> str:
    text = " ".join(str(video.get(key) or "") for key in
                    ("HDR_Format", "HDR_Format_Compatibility", "transfer_characteristics"))
    if "Dolby Vision" in text:
        return "Dolby Vision"
    if "2094" in text or "HDR10+" in text:
        return "HDR10+"
    if "2086" in text or "HDR10" in text or "PQ" in text:
        return "HDR10"
    if "HLG" in text:
        return "HLG"
    return "SDR"


def read_profile(path: str, edition: str = "") -> Profile | None:
    """What a file holds, read with mkvmerge (the tracks and their flags) and
    mediainfo (HDR, lossless or not, object audio) - None where mkvmerge
    cannot read it."""
    identified = _run_json(["mkvmerge", "-J", path])
    tracks = identified.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        return None
    info = _run_json(["mediainfo", "--Output=JSON", path])
    rows = (info.get("media") or {}).get("track") or []
    videos = [row for row in rows if row.get("@type") == "Video"]
    audio_rows = [row for row in rows if row.get("@type") == "Audio"]

    width = height = 0
    soundtracks, commentaries, others = [], [], []
    index = 0
    for track in tracks:
        properties = track.get("properties") or {}
        if track.get("type") == "video" and not width:
            wide, _x, tall = str(properties.get("pixel_dimensions")
                                 or "0x0").partition("x")
            width, height = _number(wide), _number(tall)
        if track.get("type") != "audio":
            continue
        row = audio_rows[index] if index < len(audio_rows) else {}
        commercial = str(row.get("Format_Commercial_IfAny") or "")
        name = str(properties.get("track_name") or "")
        audio = Audio(
            index, str(properties.get("language") or "und"),
            _number(properties.get("audio_channels")),
            str(row.get("Compression_Mode") or "") == "Lossless",
            "Atmos" in commercial or "DTS:X" in commercial
            or bool(_number((row.get("extra") or {}).get("NumberOfDynamicObjects"))),
            name, _number(track.get("id"), int),
            str(properties.get("language_ietf") or ""),
            _number(properties.get("audio_sampling_frequency")))
        index += 1
        if properties.get("flag_commentary") or languages.is_commentary_name(name):
            commentaries.append(audio)
        elif properties.get("flag_visual_impaired") or _other_kind(name):
            others.append(audio)
        else:
            soundtracks.append(audio)

    seconds = _number((identified.get("container") or {}).get("properties", {})
                      .get("duration"), float) / 1e9
    chapters = chapterdb.existing_chapters(path)
    names = chapters.names if chapters else ()
    return Profile(path, seconds or (chapters.seconds if chapters else 0.0),
                   width, height, _hdr(videos[0]) if videos else "SDR",
                   tuple(soundtracks), tuple(commentaries), tuple(others),
                   len(names), chapterdb.is_named(names) if names else False,
                   edition)


def _other_kind(name: str) -> str:
    """What kind of bonus audio a track name says it is, or ""."""
    for kind, pattern in _OTHER_AUDIO:
        if pattern.search(name):
            return kind
    return ""


def tier(width: int, height: int) -> int:
    """The resolution a picture is sold as: a scope film's 1920x800 is
    1080p all the same, so the width counts as much as the height."""
    for name, (wide, tall) in ((2160, (3200, 1800)), (1080, (1800, 1000)),
                               (720, (1200, 700))):
        if width >= wide or height >= tall:
            return name
    return 480


def _tier_name(profile: Profile) -> str:
    return "%dp" % tier(profile.width, profile.height) \
        if tier(profile.width, profile.height) > 480 else "SD"


def _sound(audio: Audio) -> str:
    layout = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(
        audio.channels, "%dch" % audio.channels)
    return layout + (" Atmos/DTS:X" if audio.objects else "") \
        + (" lossless" if audio.lossless else "")


def _best_per_language(tracks) -> dict:
    best: dict = {}
    for audio in tracks:
        score = (audio.channels, audio.objects, audio.lossless)
        if audio.language not in best or score > best[audio.language][0]:
            best[audio.language] = (score, audio)
    return best


def _length(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds >= 60:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%.1fs" % seconds


def editions_differ(new: Profile, old: Profile) -> str:
    """Why the two are two editions of the film, or ""."""
    if new.edition.casefold() == old.edition.casefold():
        return ""
    return "editions differ: %s vs %s" % (new.edition or "no edition",
                                          old.edition or "no edition")


def worth_measuring(new: Profile, old: Profile) -> bool:
    """Whether the two could be one cut, as far as their editions and lengths
    say: what the offset is measured for. A minute apart is still measured
    where the ratio is PAL's, which the measurement then confirms."""
    if editions_differ(new, old) or not (new.seconds and old.seconds):
        return False
    ratio = max(new.seconds, old.seconds) / min(new.seconds, old.seconds)
    return (abs(new.seconds - old.seconds) < VERSION_GAP
            or abs(ratio - PAL_SPEEDUP) < 0.004)


def different_versions(new: Profile, old: Profile,
                       timing: Timing | None = None) -> list:
    """Why the two are not one cut - nothing, where they are. ``timing``, the
    measurement that the two soundtracks line up, settles it whatever the
    lengths say."""
    reasons = []
    edition = editions_differ(new, old)
    if edition:
        reasons.append(edition)
    if timing is not None:
        return reasons
    if new.seconds and old.seconds:
        gap = new.seconds - old.seconds
        ratio = max(new.seconds, old.seconds) / min(new.seconds, old.seconds)
        if abs(ratio - PAL_SPEEDUP) < 0.004:
            reasons.append("one runs %s shorter at the 4%% PAL speed-up: likely "
                           "the same cut, but its audio would need restretching"
                           % _length(gap))
        elif abs(gap) >= VERSION_GAP:
            reasons.append("the new file runs %s %s" % (
                _length(gap), "longer" if gap > 0 else "shorter"))
    return reasons


def compare(new: Profile, old: Profile,
            timing: Timing | None = None) -> Comparison:
    new_better: list = []
    old_better: list = []

    def weigh(what: str, mine, theirs, said_new: str, said_old: str) -> None:
        if mine > theirs:
            new_better.append("%s: %s vs %s" % (what, said_new, said_old))
        elif theirs > mine:
            old_better.append("%s: %s vs %s" % (what, said_old, said_new))

    weigh("resolution", tier(new.width, new.height), tier(old.width, old.height),
          _tier_name(new), _tier_name(old))
    weigh("HDR", _HDR_RANK.get(new.hdr, 0), _HDR_RANK.get(old.hdr, 0),
          new.hdr, old.hdr)

    mine, theirs = _best_per_language(new.audio), _best_per_language(old.audio)
    for language in sorted(set(mine) | set(theirs)):
        if language not in theirs:
            new_better.append("%s audio (%s), which the existing file lacks"
                              % (language, _sound(mine[language][1])))
        elif language not in mine:
            old_better.append("%s audio (%s), which the new file lacks"
                              % (language, _sound(theirs[language][1])))
        else:
            weigh("%s audio" % language, mine[language][0], theirs[language][0],
                  _sound(mine[language][1]), _sound(theirs[language][1]))

    weigh("commentaries", len(new.commentaries), len(old.commentaries),
          _listing(new.commentaries), _listing(old.commentaries))
    kinds = {audio: _other_kind(audio.name) or "audio description"
             for audio in new.others + old.others}
    new_kinds = {kinds[audio] for audio in new.others}
    old_kinds = {kinds[audio] for audio in old.others}
    for kind in sorted(new_kinds - old_kinds):
        new_better.append("an %s track, which the existing file lacks" % kind)
    for kind in sorted(old_kinds - new_kinds):
        old_better.append("an %s track, which the new file lacks" % kind)

    weigh("chapters", (new.chapters > 0, new.named_chapters),
          (old.chapters > 0, old.named_chapters),
          _chapters(new), _chapters(old))

    versions = different_versions(new, old, timing)
    if versions:
        verdict = "different"
    elif new_better and old_better:
        verdict = "mixed"
    elif new_better:
        verdict = "new"
    elif old_better:
        verdict = "old"
    else:
        verdict = "same"
    return Comparison(verdict, new_better, old_better, versions)


def _listing(tracks) -> str:
    if not tracks:
        return "none"
    names = [audio.name or audio.language for audio in tracks]
    return "%d (%s)" % (len(tracks), ", ".join(names))


def _chapters(profile: Profile) -> str:
    if not profile.chapters:
        return "none"
    return "%d %s" % (profile.chapters,
                      "named" if profile.named_chapters else "numbered")


def audio_pair(new: Profile, old: Profile) -> tuple | None:
    """The two soundtracks the offset is measured on: one language both
    have, the same layout where there is one."""
    for audio in new.audio:
        same = [other for other in old.audio if other.language == audio.language]
        if same:
            exact = [other for other in same if other.channels == audio.channels]
            return audio, (exact or same)[0]
    if new.audio and old.audio:
        return new.audio[0], old.audio[0]
    return None


def _pcm(path: str, index: int, start: float, length: float):
    import numpy as np

    done = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", "%.3f" % max(0.0, start),
         "-i", path, "-map", "0:a:%d" % index, "-t", "%.3f" % length,
         "-ac", "1", "-ar", str(_RATE), "-f", "f32le", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return np.frombuffer(done.stdout, dtype=np.float32).astype(np.float64)


def best_lag(needle, haystack) -> tuple:
    """Where in ``haystack`` the ``needle`` fits best, in samples, and how
    well (a normalised correlation, 1 for a perfect match)."""
    import numpy as np

    if len(needle) == 0 or len(haystack) < len(needle):
        return 0, 0.0
    size = 1 << (len(haystack) + len(needle)).bit_length()
    product = np.fft.irfft(np.fft.rfft(haystack, size)
                           * np.conj(np.fft.rfft(needle, size)), size)
    lags = len(haystack) - len(needle) + 1
    sliding = np.concatenate(([0.0], np.cumsum(haystack ** 2)))
    energy = np.sqrt(np.maximum(sliding[len(needle):len(needle) + lags]
                                - sliding[:lags], 1e-12))
    scores = product[:lags] / (energy * max(np.linalg.norm(needle), 1e-12))
    lag = int(np.argmax(scores))
    return lag, float(scores[lag])


def _envelope(samples):
    """How loud the audio is, a hundred times a second, on a log scale: what
    two different mixes or encodes of one soundtrack still share where their
    waveforms no longer line up."""
    import numpy as np

    count = len(samples) // _HOP
    loudness = np.sqrt((samples[:count * _HOP].reshape(count, _HOP) ** 2).mean(1))
    loudness = np.log(loudness + 1e-4)
    return loudness - loudness.mean()


def _lag_seconds(needle, haystack) -> float | None:
    """Where the needle starts in the haystack, in seconds: found on the two
    loudness envelopes, then placed to the sample on the waveforms where
    those match closely enough to say."""
    lag, score = best_lag(_envelope(needle), _envelope(haystack))
    if score < _TRUSTED_ENVELOPE:
        return None
    coarse = lag * _HOP
    near = max(0, coarse - _REFINE)
    fine, fine_score = best_lag(needle, haystack[near:coarse + _REFINE + len(needle)])
    if fine_score >= _TRUSTED_WAVEFORM:
        return (near + fine) / _RATE
    return coarse / _RATE


def measure_offset(new: Profile, old: Profile, pair: tuple) -> Offset:
    """How far the old file's audio has to be delayed to sit in sync with the
    new one, measured at :data:`_POINTS` of the new file's length: a window of
    the new soundtrack is looked for in a wider window of the old one, wide
    enough to reach past the whole length difference."""
    new_audio, old_audio = pair
    reach = max(_REACH, abs(new.seconds - old.seconds) + _REACH)
    at, found = [], []
    for point in _POINTS:
        start = new.seconds * point
        needle = _pcm(new.path, new_audio.stream, start, _WINDOW)
        old_start = max(0.0, start - reach)
        haystack = _pcm(old.path, old_audio.stream, old_start,
                        _WINDOW + 2 * reach)
        lag = _lag_seconds(needle, haystack) if len(needle) >= _RATE else None
        at.append(start)
        found.append(None if lag is None
                     else round((start - (old_start + lag)) * 1000))
    return Offset(tuple(at), tuple(found))
