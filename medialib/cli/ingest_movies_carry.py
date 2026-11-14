"""ingest-movies's carry-over from the Plex library: what each film's copy in
the library has that the copy being ingested lacks, taken into it on the way.

Concluded EARLY, so that each film is still written once:

- The LOOK-UP starts as soon as the names have settled, on a thread of its
  own: the library is read, each film found in it (by the id the tagging will
  give it, before it has), both copies read, and the offset between their
  soundtracks measured. None of it waits for anything but the names, and it
  runs while the junk is cleaned, the tags refreshed and the commentaries
  listened for.
- Once the commentaries are flagged, it is SETTLED: each film's carry-over is
  worked out (:mod:`medialib.lib.plexcarry`), and what is a header edit is
  made then and there - the commentaries' names, so the transcription names
  its transcripts after them, and the named chapters.
- What needs PREPARING is prepared on a thread while the lanes run: a carried
  lossless track is encoded to opus in the RAM scratch (put to its speed where
  the two run at different ones), and a carried commentary with no transcript
  beside the library's copy is handed to the whisper lane. The remux lane
  waits for a film's preparation as it waits for its opus and transcripts,
  and its one mkvmerge call takes the carried tracks in.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from typing import NamedTuple

from medialib.cli import ingest_movies as rules
from medialib.cli import ingest_movies_lanes as lanes
from medialib.cli import ingest_movies_plex as plexphase
from medialib.lib import (
    bitrates,
    chapterdb,
    commentarytranscription,
    diskturn,
    plexcarry,
    plexcompare,
    plexnames,
    plexserver,
    safety,
    toolcapture,
)

__all__ = ["LibraryCarry", "Input"]


class Input(NamedTuple):
    """One extra input of the remux: ``path``, its ``track`` taken, the
    ``sync`` mkvmerge puts it in step with (``--sync``'s value without the
    track), and the :class:`plexcarry.Carried` it is - or, for a
    transcript, the carried commentary it is of with its ``language``."""

    path: str
    track: int
    sync: str
    carried: plexcarry.Carried
    language: str = ""

    @property
    def is_transcript(self) -> bool:
        return bool(self.language)


class _Found(NamedTuple):
    old: plexcompare.Profile
    timing: plexcompare.Timing | None
    untimed: str


class LibraryCarry:
    """The carry-over of every film under ``root`` from the Plex library.

    ``films`` lists the main movies the remux would improve, ``film_tags``
    is the run's :class:`lanes.FilmTags`, and ``log`` where it speaks.
    """

    def __init__(self, root: str, films, film_tags, log,
                 ram_root: str = "") -> None:
        self._root = root
        self._ram_root = ram_root
        self._films = films
        self._film_tags = film_tags
        self._log = log
        self._guard = threading.Lock()
        self._looking: threading.Thread | None = None
        self._preparing: threading.Thread | None = None
        self._said: list = []
        self._found: dict = {}
        self.plans: dict = {}
        self._inputs: dict = {}
        self._spelled: dict = {}
        self._waiting: dict = {}
        self._watchers: list = []
        self._whisper: list = []
        self._records: dict = {}
        self._left: dict = {}

    def __getstate__(self) -> dict:
        # Handed to the workers inside the run, which have no use for it.
        return {}

    def __setstate__(self, state: dict) -> None:
        LibraryCarry.__init__(self, "", list, None, lambda _line: None)

    # --- the look-up ------------------------------------------------------

    def start(self) -> None:
        self._looking = threading.Thread(target=self._look, daemon=True,
                                         name="ingest-plex-look-up")
        self._looking.start()

    def _look(self) -> None:
        server = plexserver.local()
        try:
            copies = server.copies()
        except plexserver.PlexError as error:
            self._said.append(
                "Plex carry-over: SKIPPED, the server could not be asked (%s)%s"
                % (error, "" if server.token
                   else " - set plexToken to that server's X-Plex-Token"))
            return
        index = plexphase._index(copies)
        for movie in self._films():
            if safety.abort_requested():
                return
            try:
                self._look_up(movie, index, copies)
            except Exception as error:  # noqa: BLE001 - one film, not the run
                self._said.append("Plex carry-over: WARNING, %s could not be "
                                  "looked up: %s" % (movie, error))

    def _look_up(self, movie: str, index: dict, copies: list) -> None:
        tag = ""
        if self._film_tags is not None:
            try:
                tag = self._film_tags.tag_of(movie)
            except Exception:  # noqa: BLE001 - looked for by title instead
                tag = ""
        found, _how = plexphase._found(movie, index, copies, tag)
        readable = [copy for copy in found if len(copy.files) == 1
                    and os.path.isfile(copy.files[0])]
        if not readable:
            return
        edition, part = _edition(movie)
        if part:
            return
        new = plexcompare.read_profile(movie, edition)
        if new is None:
            return
        results = []
        for copy in readable:
            old = plexcompare.read_profile(copy.files[0], copy.edition)
            if old is None:
                continue
            with diskturn.turn(copy.files[0]):
                timing, untimed = plexphase.timing_of(new, old)
            results.append(_Found(old, timing, untimed))
        with self._guard:
            self._found[plexphase.folder_key(movie)] = results

    # --- settling ---------------------------------------------------------

    def settle(self, keeps_for, rename, whisper: bool) -> None:
        """Every film's carry-over worked out, its names and chapters written
        in place, and its preparation started. ``keeps_for`` says, of a
        film, which soundtrack languages its improved copy keeps; ``rename``
        gives a film's commentaries the names a plan says; ``whisper`` is
        whether the whisper lane will run, to transcribe a commentary with no
        transcript."""
        if self._looking is not None:
            self._looking.join()
        while self._said:
            self._log(self._said.pop(0))
        preparing = []
        for movie in self._films():
            if safety.abort_requested():
                return
            found = self._found.get(plexphase.folder_key(movie))
            if not found:
                continue
            plan = self._plan(movie, found, keeps_for)
            if plan is None or not (plan.anything or plan.left):
                continue
            self._log(("Carrying over from the library's copy: "
                       if plan.anything else
                       "Nothing carried over from the library's copy: ")
                      + movie)
            self._log("  " + plan.library)
            if plan.timing is not None:
                self._log("  " + plan.timing.describe(_seconds(movie)))
            for line in plan.carried:
                self._log("  carried: " + line)
            for line in plan.left:
                self._log("  not carried: " + line)
            if plan.names:
                failure = rename(movie, list(plan.names))
                if failure:
                    self._log("  WARNING: the commentaries were not named: "
                              + failure)
            if plan.chapters is not None and not chapterdb.write_chapters(
                    movie, plan.chapters):
                self._log("  WARNING: the chapters could not be written")
            self.plans[plexphase.folder_key(movie)] = plan
            if plan.audio:
                preparing.append((movie, plan))
        for movie, plan in preparing:
            film = lanes.key(movie)
            self._spelled[film] = movie
            self._waiting[film] = {"encode"}
            if whisper and any(carried.kind == "commentary"
                               and not carried.transcripts
                               for carried in plan.audio):
                self._waiting[film].add("whisper")
                self._whisper.append((movie, plan))
        self._preparing = threading.Thread(
            target=self._prepare, args=(preparing,), daemon=True,
            name="ingest-plex-carry")
        self._preparing.start()

    def _plan(self, movie: str, found: list, keeps_for):
        """The carry-over that takes the most, of each library copy's."""
        edition, _part = _edition(movie)
        new = plexcompare.read_profile(movie, edition)
        if new is None:
            return None
        keeps = keeps_for(movie)
        best = None
        for item in found:
            plan = plexcarry.plan(
                new, item.old, item.timing, item.untimed, keeps,
                plexphase.transcripts_beside(item.old.path),
                chapterdb.existing_chapters(item.old.path))
            if best is None or len(plan.carried) > len(best.carried):
                best = plan
        return best

    # --- preparing --------------------------------------------------------

    def watch(self, done) -> set:
        """The films still being prepared, as :func:`lanes.key` spells
        them; ``done`` is told each one as it is ready."""
        with self._guard:
            self._watchers.append(done)
            return set(self._waiting)

    def _done(self, film: str, what: str) -> None:
        with self._guard:
            left = self._waiting.get(film)
            if left is None:
                return
            left.discard(what)
            if left:
                return
            del self._waiting[film]
            watchers = list(self._watchers)
        for watcher in watchers:
            watcher(self._spelled[film])

    def _prepare(self, preparing: list) -> None:
        for movie, plan in preparing:
            film = lanes.key(movie)
            try:
                if not safety.abort_requested():
                    self._inputs[film] = self._inputs_of(movie, plan)
            except Exception as error:  # noqa: BLE001 - one film, not the run
                self._log("WARNING: the carry-over of %s could not be prepared: "
                          "%s" % (movie, error))
            self._done(film, "encode")

    def _inputs_of(self, movie: str, plan: plexcarry.Plan) -> list:
        """A film's carried tracks as remux inputs, each lossless one encoded
        to opus in the RAM scratch on the way."""
        timing = plan.timing
        if timing is None:
            return []
        inputs = []
        delay = round(timing.start * 1000)
        for carried in plan.audio:
            audio = carried.audio
            if carried.encode:
                opus = self.opus_for(self._ram_root, movie, audio)
                if self._encode(plan.library, audio, opus, carried.stretch,
                                timing.speed):
                    inputs.append(Input(opus, 0, "%d" % delay, carried))
                    continue
                if carried.stretch:
                    self._log("  WARNING: %s could not be encoded to the new "
                              "file's speed: not carried: %s"
                              % (_said(audio), movie))
                    continue
            # A lossy track, or a lossless one opus cannot take as it is,
            # muxed straight out of the library's copy.
            inputs.append(Input(plan.library, audio.track, "%d" % delay,
                                carried))
        return inputs

    @staticmethod
    def opus_for(ram_root: str, movie: str, audio: plexcompare.Audio) -> str:
        """Where a carried track's opus waits for the remux: in the RAM
        scratch beside the film's own (:func:`rules.opus_path`), and let go
        of with them."""
        return rules.opus_path(ram_root, movie, "library%d" % audio.track)

    def _encode(self, library: str, audio: plexcompare.Audio, opus: str,
                stretch: bool, speed: float) -> bool:
        channels = str(audio.channels)
        bitrate = bitrates.audio_bitrate(channels, "normal")
        if not bitrate:
            return False
        encoding = rules.opus_encoding(library, audio.stream, channels, bitrate)
        if isinstance(encoding, str):
            return False
        layout, channels, bitrate = encoding
        filters = []
        if layout[:1] == ["-af"]:
            filters.append(layout[1])
            layout = []
        if stretch:
            if not audio.rate:
                return False
            # Played faster or slower the way a copy at another frame rate
            # was made: the pitch moves with the speed, and back again here.
            filters += ["asetrate=%.6f" % (audio.rate / speed),
                        "aresample=%d" % audio.rate]
        if os.path.isfile(opus):
            # Resumed: an opus a run before this one finished.
            return True
        os.makedirs(os.path.dirname(opus), exist_ok=True)
        partial = opus + ".part"
        with diskturn.turn(library):
            self._log("Encoding the library's %s to opus (%sk, %sch)%s"
                      % (_said(audio), bitrate, channels,
                         ", put to the new file's speed" if stretch else ""))
            done = toolcapture.watched(
                ["ffmpeg", "-y", "-loglevel", "error", "-nostats",
                 "-i", library, "-vn", "-map", "0:a:%d" % audio.stream,
                 "-map_metadata", "-1", "-c:a", "libopus", "-b:a",
                 bitrate + "k"] + layout
                + (["-af", ",".join(filters)] if filters else [])
                + ["-f", "opus", partial],
                announce=False, skipped="the track was not carried",
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        if done.returncode != 0:
            rules._remove(partial)
            return False
        os.replace(partial, opus)
        return True

    # --- the whisper lane's part -------------------------------------------

    def transcription_records(self, ram_root: str, whisper: dict, log):
        """The transcriptions of the carried commentaries with no transcript
        beside the library's copy, as the whisper lane takes them: the track
        extracted into the scratch, its language heard, and a record queued
        for each transcript it wants."""
        for movie, plan in self._whisper:
            film = lanes.key(movie)
            count = 0
            for carried in plan.audio:
                if carried.kind != "commentary" or carried.transcripts:
                    continue
                for record, size in self._records_of(movie, plan, carried,
                                                     ram_root, whisper, log):
                    with self._guard:
                        self._records[record] = film
                        self._left[film] = self._left.get(film, 0) + 1
                    count += 1
                    yield record, size
            if not count:
                self._done(film, "whisper")

    def _records_of(self, movie, plan, carried, ram_root, whisper, log):
        audio = carried.audio
        staged = self.staged_for(ram_root, movie, audio)
        os.makedirs(os.path.dirname(staged), exist_ok=True)
        mka = staged + ".mka"
        with diskturn.turn(plan.library):
            made = toolcapture.watched(
                ["ffmpeg", "-y", "-loglevel", "error", "-nostats",
                 "-i", plan.library, "-vn", "-map", "0:a:%d" % audio.stream,
                 "-acodec", "copy", mka],
                announce=False, skipped="the commentary was not transcribed",
                stdin=subprocess.DEVNULL)
        if made.returncode != 0:
            rules._remove(mka)
            return
        spec, code = commentarytranscription.commentary_language(
            audio.name, audio.language, mka, ram_root, whisper,
            lambda line: log("  " + line))
        if not spec:
            spec, code = "en", "en"
        jobs = [("transcribe", staged + ".en.srt")] if code == "en" else (
            ([("transcribe", "%s.%s.srt" % (staged, code))] if code else [])
            + [("translate", staged + ".en.srt")])
        log("Queued the library's %s to be transcribed: %s"
            % (_said(audio), movie))
        siblings = "\x1e".join(srt for _task, srt in jobs)
        size = os.path.getsize(mka)
        for task, srt in jobs:
            yield "\x1f".join([mka, srt, task, spec, siblings]), size

    @staticmethod
    def staged_for(ram_root: str, movie: str, audio: plexcompare.Audio) -> str:
        """Where a carried commentary's transcripts are made, before the remux
        takes them in and names them beside the film."""
        film = hashlib.sha1(lanes.key(movie).encode("utf-8",
                                                    "surrogateescape"))
        return os.path.join(ram_root, "carry", film.hexdigest()[:16],
                            "track%d" % audio.track)

    def owns(self, record: str) -> bool:
        with self._guard:
            return record in self._records

    def record_done(self, record) -> None:
        with self._guard:
            film = self._records.pop(record, None)
            if film is None:
                return
            self._left[film] -= 1
            if self._left[film]:
                return
        self._done(film, "whisper")

    def finish(self) -> None:
        """Wait for the preparation, and let go of every film still waiting:
        the walk after the lanes remuxes it with what there is."""
        if self._preparing is not None:
            self._preparing.join()
        with self._guard:
            waiting = list(self._waiting)
        for film in waiting:
            self._done(film, "whisper")

    # --- the remux's part -----------------------------------------------------

    def inputs_for(self, movie: str, ram_root: str = "") -> list:
        """The film's carried tracks and transcripts, as remux inputs."""
        film = lanes.key(movie)
        inputs = list(self._inputs.get(film, ()))
        plan = self.plans.get(plexphase.folder_key(movie))
        if not inputs or plan is None or plan.timing is None:
            return inputs
        sync = "%d,%.9f" % (round(plan.timing.start * 1000), plan.timing.speed)
        for item in list(inputs):
            carried = item.carried
            if carried.kind != "commentary":
                continue
            found = carried.transcripts
            if not found and ram_root:
                staged = self.staged_for(ram_root, movie, carried.audio)
                found = tuple((path, path[:-len(".srt")].rpartition(".")[2])
                              for path in _srts_beginning(staged))
            for srt, language in found:
                inputs.append(Input(srt, 0, sync, carried, language))
        return inputs

    def plan_for(self, movie: str):
        return self.plans.get(plexphase.folder_key(movie))


def _srts_beginning(staged: str) -> list:
    directory, prefix = os.path.split(staged)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, name) for name in names
            if name.startswith(prefix + ".") and name.endswith(".srt")]


def _edition(movie: str) -> tuple:
    base, _tag = plexnames.untagged_base(
        os.path.basename(os.path.dirname(os.path.abspath(movie))))
    return plexnames.read_stem(base, os.path.splitext(
        os.path.basename(movie))[0])


def _seconds(movie: str) -> float:
    existing = chapterdb.existing_chapters(movie)
    return existing.seconds if existing else 0.0


def _said(audio: plexcompare.Audio) -> str:
    return ('"%s"' % audio.name if audio.name
            else "%s track" % audio.language)
