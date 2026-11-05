"""ingest-movies's lanes: the work on the films run side by side, each kind on
the part of the machine it uses.

The middle of a full ingest is four kinds of work on every film, and each
leans on something else: the opus transcode on the CPU, the commentary
transcription on the GPU, the improved-copy remux on the disk, the subtitle
download on the network. Run one after the other, each leaves the other three
idle for as long as it lasts. Here they overlap where that is safe:

- The REMUX LANE remuxes a film as soon as its own opus tracks and
  transcripts are done, one film at a time on a thread of its own, while the
  transcode and the transcription go on with the rest. A film it never heard
  was ready is remuxed by the walk after it, the way every film used to be.
- The SUBTITLE LANE asks TMDb which film each folder is - a dry run of the
  tagging the run does at its end, its answers remembered for that - and
  downloads the missing subtitles of every film it got an id for, one film
  after another, polite as the download always was. It is finished before
  the tagging renames anything; what it did not get to is downloaded after
  the tagging, the way it always was.

What keeps the overlap from costing more than it saves:

- A disk-bound job takes the disk's turn (:mod:`medialib.lib.diskturn`), so
  a spinning disk is never asked to read two films at once.
- A film is held by one lane at a time (:class:`FilmLocks`): the remux
  swaps the file out from under its name, and a subtitle synced against a
  film halfway through being swapped would be synced against nothing.
- A remux into the RAM scratch while the transcription is still extracting
  commentary into it sizes itself with room to spare for those extracts.
"""

from __future__ import annotations

import contextlib
import os
import queue
import threading

from medialib.lib import (
    diskturn,
    errorlog,
    runlog,
    safety,
    subtitlefiles,
    tmdblookup,
)

__all__ = ["FilmLocks", "RemuxLane", "SubtitleLane", "TranscriptTracker",
           "key"]

# The RAM a remux leaves free while the transcription may still be extracting
# commentary into the same scratch: a few commentary tracks' worth, each a
# couple of gigabytes at the most for a long film's DTS.
TRANSCRIBING_RAM_HEADROOM = 8 * 1024 ** 3


def key(path: str) -> str:
    """A film as every lane names it: absolute and normalised, so the walks
    that spell it from different starting points agree on it."""
    return os.path.normpath(os.path.abspath(path))


class FilmLocks:
    """One lock per film, for the lanes that must not work on it at once.

    Taken BEFORE the disk's turn, always: a lane holding a film's lock and
    waiting for the disk, and another holding the disk and waiting for that
    film, would wait for each other forever."""

    def __init__(self) -> None:
        self._locks: dict = {}
        self._guard = threading.Lock()

    @contextlib.contextmanager
    def held(self, movie: str):
        with self._guard:
            lock = self._locks.setdefault(key(movie), threading.Lock())
        with lock:
            yield


class RemuxLane:
    """Films remuxed one at a time on a thread of their own, each as soon as
    everything it waits for is done.

    ``films`` are the main movies the remux walk would improve, as it spells
    them. A film waits for its opus transcode always, and for its transcripts
    when ``transcripts`` is True; :meth:`opus_done` and :meth:`transcripts_done`
    say a film's are, from whichever thread finished them. ``improve`` is the
    remux of one film. :attr:`done` are the films this lane has seen to,
    whatever came of each, for the walk after it to pass over.
    """

    def __init__(self, films: list, improve, locks: FilmLocks,
                 transcripts: bool, log) -> None:
        self._films = {key(film): film for film in films}
        self._improve = improve
        self._locks = locks
        self._log = log
        self._guard = threading.Lock()
        self._waiting_opus = set(self._films)
        self._waiting_transcripts = set(self._films) if transcripts else set()
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._said = False
        self.done: set = set()

    def opus_done(self, movie) -> None:
        if movie is None:
            return
        with self._guard:
            self._waiting_opus.discard(key(movie))
        self._maybe(key(movie))

    def transcripts_done(self, movie: str) -> None:
        with self._guard:
            self._waiting_transcripts.discard(key(movie))
        self._maybe(key(movie))

    def _maybe(self, film: str) -> None:
        with self._guard:
            if (film not in self._films or film in self._waiting_opus
                    or film in self._waiting_transcripts):
                return
            # Queued once: a film both of whose waits end is not queued
            # again by the second.
            spelled = self._films.pop(film)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="ingest-remux-lane", daemon=True)
                self._thread.start()
        self._queue.put(spelled)

    def _run(self) -> None:
        while True:
            film = self._queue.get()
            if film is None:
                return
            if safety.abort_requested():
                continue
            if not self._said:
                self._said = True
                self._log("Remux lane: improving main movies as their tracks "
                          "and transcripts are ready")
            try:
                with runlog.held(), errorlog.work_item(film), \
                        self._locks.held(film), diskturn.turn(film):
                    self._improve(film)
            except Exception as error:  # noqa: BLE001 - one film, not the run
                self._log("WARNING: the remux lane could not improve %s: %s"
                          % (film, error))
            # Seen to whatever came of it: the walk after the lane would only
            # do the same thing again.
            self.done.add(key(film))

    def finish(self) -> None:
        """Remux every film that is ready and wait for it; a film still waiting
        is left to the walk after the lane."""
        with self._guard:
            thread = self._thread
        if thread is None:
            return
        self._queue.put(None)
        thread.join()


class TranscriptTracker:
    """Which film each queued transcription is of, and so when a film's
    transcripts are all done.

    The commentary walk queues one film's transcriptions after another and
    says when it has queued the last of a film's (:meth:`film_queued`); the
    queue says when each transcription is done (:meth:`record_done`), on its
    own thread and in any order. A film is done once it has been queued and
    every transcription queued for it is done - at once, for one with none."""

    def __init__(self, done) -> None:
        self._done = done
        self._guard = threading.Lock()
        self._unclaimed: list = []
        self._finished: set = set()
        self._film_of: dict = {}
        self._left: dict = {}

    def record_queued(self, record: str) -> None:
        with self._guard:
            self._unclaimed.append(record)

    def film_queued(self, movie: str) -> None:
        with self._guard:
            records, self._unclaimed = self._unclaimed, []
            left = {record for record in records
                    if record not in self._finished}
            self._finished.difference_update(records)
            if left:
                self._left[key(movie)] = left
                for record in left:
                    self._film_of[record] = key(movie)
                return
        self._done(movie)

    def record_done(self, record) -> None:
        if record is None:
            return
        with self._guard:
            film = self._film_of.pop(record, None)
            if film is None:
                # Finished before the walk said which film it was of.
                self._finished.add(record)
                return
            left = self._left[film]
            left.discard(record)
            if left:
                return
            del self._left[film]
        self._done(film)


class SubtitleLane:
    """The subtitle download, run ahead on a thread of its own.

    ``films`` are the films the download walk would fetch for. The thread
    first asks TMDb, in a dry run of the tagging, which film each folder is,
    and then downloads the missing subtitles of every film it now has an id
    for, by that id, one film at a time. ``download`` is
    :func:`subtitlefiles.download_subs_for` with the run's settings bound.
    :meth:`finish` waits for it, and must be called before anything renames
    the films.
    """

    def __init__(self, root: str, films: list, download, locks: FilmLocks,
                 log) -> None:
        self._root = root
        self._films = films
        self._download = download
        self._locks = locks
        self._log = log
        self._thread = threading.Thread(target=self._run,
                                        name="ingest-subtitle-lane",
                                        daemon=True)
        self._said: list = []

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            ids = self._ids()
            for movie in self._films:
                if safety.abort_requested():
                    return
                imdb = ids.get(key(os.path.dirname(movie)), "")
                if not imdb:
                    imdb = subtitlefiles.film_imdb_id(movie)
                if not imdb:
                    continue
                with runlog.held(), self._locks.held(movie):
                    self._download([(movie, imdb)])
        except Exception as error:  # noqa: BLE001 - the walk after it remains
            self._said.append("WARNING: the subtitles fetched ahead stopped "
                              "early (%s); the rest are fetched after the "
                              "tagging" % error)

    def _ids(self) -> dict:
        """{folder: IMDb id} for every film folder the tagging would name,
        asked in a dry run that says nothing and renames nothing."""
        tagged: dict = {}
        tmdblookup.tag_plex_ids(self._root, _quiet, safety.SkipLog(),
                                dry_run=True, tagged=tagged,
                                long_names=tmdblookup.LongNames())
        ids = {}
        for tag, folders in tagged.items():
            imdb = subtitlefiles.imdb_of_name(tag)
            for folder in folders:
                if imdb:
                    ids[key(folder)] = imdb
        return ids

    def finish(self) -> None:
        if self._thread.is_alive():
            self._log("Waiting for the subtitles fetched ahead to finish")
        if self._thread.ident is not None:
            self._thread.join()
        for line in self._said:
            self._log(line)


def _quiet(_line: str) -> None:
    """The dry run's voice: it says what it would rename, which is the
    tagging's to say when it does."""
