"""Telling an audio commentary the file does not call one from the tracks it
sits among.

A disc's commentary often reaches the library without its flag and without a
name that says so - "English", "Stereo", nothing at all. It then looks like a
second soundtrack: it is not transcribed, and the surround ladder may drop it as
a lesser copy of the main mix. What gives it away is the film's own first audio
track, listened to over the same few minutes:

* a commentary is talked over from end to end - far more of it is speech than
  of the film, which leaves gaps between its lines;
* and what is said in it is mostly NOT what the film says: the film's dialogue
  is ducked under the commentators' voices, so most of the words whisper hears
  in it are words the main track never has.

A downmix or the original mono of the same film shares both its speech and its
words with the main track; an isolated score has no speech at all; a dub has
the film's speech, in another language. A track is called a commentary only
when BOTH signals say so. When only one does it is left as it is and said, for
somebody to listen to - a wrong flag would send a soundtrack to transcription
and out of the surround ladder, while a missed commentary is only a commentary
the file did not flag, as before.

The measurement is one :data:`PROBE_COMMAND` run per film: three windows of
:data:`PROBE_SECONDS`, read with a seek each rather than the whole file, of the
main track and of every suspect.
"""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from typing import NamedTuple

from medialib import helpers

__all__ = [
    "PROBE_COMMAND",
    "PROBE_SECONDS",
    "PROBE_WINDOWS",
    "MIN_SPEECH",
    "MIN_SPEECH_GAIN",
    "MIN_NEW_WORDS",
    "COMMENTARY",
    "NOT_COMMENTARY",
    "UNSURE",
    "AudioTrack",
    "Measure",
    "Verdict",
    "suspects",
    "words",
    "judge",
    "probe",
    "commentary_names",
]

PROBE_COMMAND = ("pipx", "run", helpers.path_of("commentary_probe.py"))

# Where the windows start, as a share of the running time, and how long each
# is. The middle of the film rather than its ends: a commentary opens on the
# studio logos and often closes over the credits, where it may well be silent.
PROBE_WINDOWS = (0.25, 0.5, 0.75)
PROBE_SECONDS = 60

# The two signals, and the floor under the first. Measured on real discs: a
# commentary was speech 0.87-0.97 of the time where its film was 0.17-0.81, the
# closest pair 0.94 against 0.81; every downmix, original mono and dub came
# within 0.01 of its film. Of a commentary's words 0.62-1.00 were missing from
# the film's transcript, of theirs 0.02-0.11.
MIN_SPEECH = 0.6
MIN_SPEECH_GAIN = 0.10
MIN_NEW_WORDS = 0.5

COMMENTARY = "commentary"
NOT_COMMENTARY = "not"
UNSURE = "unsure"

# Words in a track's name that already say what it is, and that it is not a
# commentary: an audio description shares the film's dialogue AND adds a voice,
# which is the one kind of track the two signals could split on, and a score or
# an effects track is a track somebody took the trouble to name.
_NAMED_AS_OTHER = re.compile(
    r"descri|deskri|\bad\b|isolated|score|music|musik|musique|effects",
    re.IGNORECASE)

# Scripts written without spaces between words, where a "word" whisper prints
# is a whole phrase: they are compared by their pairs of characters instead.
_UNSPACED = re.compile(
    "[฀-๿຀-໿က-႟ក-៿"
    "぀-ヿ㐀-䶿一-鿿豈-﫿]")


class AudioTrack(NamedTuple):
    """One audio track of the film, as the caller read it: its mkvmerge id, its
    0-based audio index (ffmpeg's ``-map 0:a:N``), its channel count as
    mkvmerge prints it, its name, whether the file already calls it a
    commentary, and whether it is flagged for the visually impaired."""

    id: str
    audio_index: int
    channels: str
    name: str
    commentary: bool
    visual_impaired: bool


class Measure(NamedTuple):
    """What the probe heard in one track over the windows: the share of them
    that is speech, whisper's transcript, and the language it heard."""

    speech: float
    text: str
    language: str


class Verdict(NamedTuple):
    """One suspect judged: :data:`COMMENTARY`, :data:`NOT_COMMENTARY` or
    :data:`UNSURE`, and the figures it was judged on, to be said."""

    kind: str
    why: str


def suspects(audio: list[AudioTrack]) -> list[AudioTrack]:
    """The audio tracks that might be a commentary the file does not call one.

    Never the first audio track, which is the film, and the one the others are
    held against; never a track the file already calls a commentary, nor one
    flagged or named as something else. A commentary is two channels at most,
    so a surround track is not one either.
    """
    found = []
    for track in audio[1:]:
        if track.commentary or track.visual_impaired:
            continue
        if _NAMED_AS_OTHER.search(track.name or ""):
            continue
        try:
            channels = int(track.channels)
        except ValueError:
            continue
        if 0 < channels <= 2:
            found.append(track)
    return found


def words(text: str) -> set[str]:
    """The words of a transcript worth comparing: those of four letters or
    more, folded, so "the" and "and" do not make two unrelated tracks look
    alike - and in a script written without spaces, each pair of characters."""
    found: set[str] = set()
    folded = unicodedata.normalize("NFKC", text).casefold()
    for word in re.findall(r"\w+", folded):
        if _UNSPACED.search(word):
            found.update(word[i:i + 2] for i in range(len(word) - 1))
        elif len(word) >= 4 and not word.isdigit():
            found.add(word)
    return found


def judge(main: Measure, suspect: Measure) -> Verdict:
    """Whether ``suspect`` is a commentary, held against the film's ``main``
    track over the same windows. See the module's description for why."""
    theirs = words(suspect.text)
    film = words(main.text)
    new = len(theirs - film) / len(theirs) if theirs else 0.0
    gain = suspect.speech - main.speech
    why = ("speech %.2f against the film's %.2f, %d%% of its words not the "
           "film's" % (suspect.speech, main.speech, round(new * 100)))

    talked_over = suspect.speech >= MIN_SPEECH and gain >= MIN_SPEECH_GAIN
    other_words = new >= MIN_NEW_WORDS
    if talked_over and other_words:
        return Verdict(COMMENTARY, why)
    if not talked_over and not other_words:
        return Verdict(NOT_COMMENTARY, why)
    # Words of its own without more speech than the film is a dub, as long as
    # it is in a language of its own too.
    if (other_words and not talked_over and suspect.language
            and main.language and suspect.language != main.language):
        return Verdict(NOT_COMMENTARY, why + ", in another language")
    return Verdict(UNSURE, why)


def probe(movie: str, main: AudioTrack, others: list[AudioTrack],
          whisper: dict) -> tuple[dict[str, Measure], str]:
    """The measures of ``main`` and every one of ``others``, by track id, with
    "" - or ({}, why not). ``whisper`` is the run's settled whisper; the
    multilingual model is the one used, as the language is part of the
    answer."""
    tracks = [main] + others
    argv = list(PROBE_COMMAND) + [
        "--model", whisper["modelMulti"], "--device", whisper["device"],
        "--compute_type", whisper["computeType"],
        "--threads", str(whisper["threads"]),
        "--seconds", str(PROBE_SECONDS)]
    for at in PROBE_WINDOWS:
        argv += ["--at", str(at)]
    argv += [movie] + [str(track.audio_index) for track in tracks]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              capture_output=True)
    except OSError as error:
        return {}, str(error)
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        return {}, "the probe failed (%d)%s" % (
            done.returncode, ": " + tail[-1] if tail else "")
    try:
        answer = json.loads(done.stdout.decode("utf-8", "replace"))
        measures = {}
        for track in tracks:
            heard = answer[str(track.audio_index)]
            measures[track.id] = Measure(float(heard["speech"]),
                                         str(heard["text"]),
                                         str(heard["language"]))
    except (ValueError, KeyError, TypeError):
        return {}, "the probe's answer could not be read"
    return measures, ""


def commentary_names(audio: list[AudioTrack], found: list[str]) -> dict:
    """The name each newly found commentary (by id, in ``found``) is given:
    "Commentary N", N its place among the film's commentaries in file order -
    the numbered name the commentary naming later replaces with who speaks."""
    names = {}
    place = 0
    for track in audio:
        if track.commentary or track.id in found:
            place += 1
            if track.id in found:
                names[track.id] = "Commentary %d" % place
    return names
