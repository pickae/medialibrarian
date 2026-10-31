"""The lane: one owner of the encoders for a run made of many small runs.

convert-comics converts a book at a time, each through a convert-images of its
own. Were each of those to call av1gpu it would pay the encoder's start-up of
about 2 s per book, and several would share the card at once for no gain
(medialib/lib/av1gpu.py has the measurements); and were each to encode its own
CPU pages, a book of pages too large for the card would hold its worker for
minutes while the card waited for the next book. So under a lane the per-book
runs only SORT their pages - the card's, losslessly cropped where the trim cut
them, and the CPU's, each with its ImageMagick call - into a manifest in a
folder of their own, and the lane, threads in the run's own process, encodes
them, pages from every book ready at once:

- the card's pages in chunks, one av1gpu call started the moment the card has
  nothing to do and something is waiting, a second beside it once a full
  chunk is waiting - av1gpu reads its whole list before it starts, so the
  second call's start-up is paid while the first is still encoding;
- the CPU's pages on ``cpu_jobs`` encoders, which take one of the card's
  pages when they have none of their own and the card has more than a chunk
  waiting: the whole machine works until the last page.

A book is handed back to the run - to be numbered and zipped - once every page
of it has an output: written by the card, or by the ImageMagick call its
manifest line names. A page the card did not write goes to that call; a card
whose call wrote nothing and failed could not start the encoder at all, and
every later page goes straight to the CPU.

The run unpacks the next book whenever either side is short of work, and
:meth:`Lane.wait_for_room` says which: the run then takes the book likeliest to
feed that side, so a stretch of books with nothing for the card does not leave
it idle while books further on would feed it. The card comes first - when
both are short it is the card's book that is unpacked - and the CPU's encodes
run niced, below the sorting that feeds the card. What bounds the looking
ahead is RAM, read live: every book unpacked and not yet zipped is held there,
and none is unpacked while less than :data:`RAM_RESERVE` is left.
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from medialib.lib import av1gpu, safety

__all__ = ["READY", "MANIFEST", "Lane"]

# What a per-book run leaves in its lane folder: the manifest it appended a line
# to per page, and READY once it has exited - before then the manifest may
# still grow.
MANIFEST = "manifest"
READY = "ready"

# Pages a call, in megapixels: what a fresh call takes at most. Far below
# av1gpu.CHUNK_MEGAPIXELS, because a book is only finished once its LAST page is
# out of the card, and every page waiting for that is a page in RAM.
CHUNK_MEGAPIXELS = 3000

# Calls at once: the second hides the first one's start-up and draining tail.
CALLS = 2

# Work waiting, below which the run unpacks another book: chunks for the card,
# pages per encoder for the CPU.
QUEUED_CHUNKS = 2
QUEUED_PER_ENCODER = 2

# RAM kept free of unpacked books: what the encoders themselves need - an
# ImageMagick AVIF encode of a large page takes a few GB - and room for the
# book being unpacked.
RAM_RESERVE = 16 * 1024 ** 3

# The CPU's encodes run below the sorting that feeds the card: with every core
# taken by encoders, the next books' pages were sorted - and the card fed -
# only as fast as the scheduler's fair share let them, and the card sat idle
# between chunks.
NICE = ["nice", "-n", "10"] if shutil.which("nice") else []

_POLL_SECONDS = 0.2


class _Book:
    def __init__(self, folder: str, item) -> None:
        self.folder = folder
        self.item = item
        self.outstanding = 0


class Lane:
    """The lane for one run. <encoder> is av1gpu, or "" to encode every page
    on the CPU. <finish> is called with a book's item, on a thread of the
    lane's, once all its pages have outputs; <cpu_jobs> is how many CPU
    encodes run at once."""

    def __init__(self, encoder: str, root: str, quality: int, preset: str,
                 chroma: str, finish, cpu_jobs: int, log) -> None:
        self.encoder = encoder
        self.root = root
        self.quality = quality
        self.preset = preset
        self.chroma = chroma
        self.finish = finish
        self.log = log
        self.cpu_jobs = max(1, cpu_jobs)
        self.budget = CHUNK_MEGAPIXELS * 1_000_000
        self.room = threading.Condition()
        self.expected: dict[str, _Book] = {}
        self.dropped: list[_Book] = []
        self.pending: list = []
        self.pending_pixels = 0
        self.cpu_queue: collections.deque = collections.deque()
        self.running = 0
        self.closing = False
        self.broken = not encoder
        # What the closing report says.
        self.calls = 0
        self.pages = 0
        self.pixels = 0
        self.cpu_pages = 0
        self.cpu_pixels = 0
        self.stolen = 0
        self.fallbacks = 0
        self.busy = 0.0
        self._busy_since = 0.0
        self.finishers = ThreadPoolExecutor(max_workers=2)
        self.threads = [threading.Thread(target=self._watch, daemon=True,
                                         name="lane-watch")]
        if encoder:
            self.threads += [threading.Thread(target=self._feed, daemon=True,
                                              name="lane-gpu-%d" % n)
                             for n in range(CALLS)]
        self.threads += [threading.Thread(target=self._encode, daemon=True,
                                          name="lane-cpu-%d" % n)
                         for n in range(self.cpu_jobs)]
        for thread in self.threads:
            thread.start()

    # --- the run's side ---------------------------------------------------
    def folder(self) -> str:
        """A new lane folder for one book."""
        return tempfile.mkdtemp(prefix="book_", dir=self.root)

    def expect(self, folder: str, item) -> None:
        """Wait for the book in <folder> to be READY; <item> is what <finish>
        is handed once it is done."""
        with self.room:
            self.expected[folder] = _Book(folder, item)

    def wait_for_room(self) -> str:
        """Until another book is worth unpacking - a side short of work, and
        RAM to hold it - and which side it is for: "gpu" or "cpu", the card
        first. "" once the run is stopping."""
        with self.room:
            while not safety.abort_requested():
                gpu = (not self.broken
                       and self.pending_pixels < QUEUED_CHUNKS * self.budget)
                cpu = len(self.cpu_queue) < QUEUED_PER_ENCODER * self.cpu_jobs
                if (gpu or cpu) and self._ram_free() > RAM_RESERVE:
                    return "gpu" if gpu else "cpu"
                self.room.wait(_POLL_SECONDS)
        return ""

    def _ram_free(self) -> int:
        """What is left for another book: the RAM the kernel says is
        available, and the scratch filesystem's own free space, whichever is
        less. Both are read afresh - other programs use the machine too."""
        stat = os.statvfs(self.root)
        free = stat.f_bavail * stat.f_frsize
        try:
            with open("/proc/meminfo") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        free = min(free, int(line.split()[1]) * 1024)
                        break
        except (OSError, ValueError):
            pass
        return free

    def close(self) -> None:
        """No more books: every one still expected is collected, both sides
        drained, and each book finished, before this returns. A book whose run
        never said it was ready had its worker fail, which the queue has
        already reported; its pages are dropped with it."""
        with self.room:
            self.closing = True
            self.room.notify_all()
        for thread in self.threads:
            thread.join()
        self.finishers.shutdown(wait=True)
        for book in self.dropped:
            shutil.rmtree(book.folder, ignore_errors=True)

    # --- collecting ready books --------------------------------------------
    def _watch(self) -> None:
        while True:
            with self.room:
                closing = self.closing
                waiting = list(self.expected.values())
            for book in waiting:
                if os.path.exists(os.path.join(book.folder, READY)):
                    self._collect(book)
            if closing:
                with self.room:
                    self.dropped = list(self.expected.values())
                    self.expected.clear()
                    self.room.notify_all()
                return
            time.sleep(_POLL_SECONDS)

    def _collect(self, book: _Book) -> None:
        entries = []
        try:
            with open(os.path.join(book.folder, MANIFEST)) as handle:
                entries = [json.loads(line) for line in handle if line.strip()]
        except (OSError, ValueError):
            pass
        with self.room:
            del self.expected[book.folder]
            book.outstanding = len(entries)
            for entry in entries:
                if "input" in entry and not self.broken:
                    self.pending.append((book, entry))
                    self.pending_pixels += entry["pixels"]
                else:
                    self.cpu_queue.append((book, entry))
            self.room.notify_all()
        if not entries:
            self._finished(book)

    # --- the card -----------------------------------------------------------
    def _take(self):
        """The next chunk, or None once there is no more for the card. Waits
        while there is nothing worth a call."""
        with self.room:
            while True:
                if safety.abort_requested() or self.broken:
                    return None
                if self.pending and (self.running == 0
                                     or self.pending_pixels >= self.budget
                                     or self.closing):
                    break
                if self.closing and not self.pending and not self.expected:
                    return None
                self.room.wait(_POLL_SECONDS)
            chunk, filled = [], 0
            while self.pending and (not chunk or filled + self.pending[0][1][
                    "pixels"] <= self.budget):
                book, entry = self.pending.pop(0)
                chunk.append((book, entry))
                filled += entry["pixels"]
            self.pending_pixels -= filled
            if self.running == 0:
                self._busy_since = time.time()
            self.running += 1
            self.room.notify_all()
            return chunk

    def _feed(self) -> None:
        while True:
            chunk = self._take()
            if chunk is None:
                break
            items = [(entry["input"], entry["output"]) for _book, entry in chunk]
            workdir = tempfile.mkdtemp(prefix="av1gpu_", dir=self.root)
            try:
                written, status = av1gpu.encode_chunk(
                    self.encoder, items, workdir, self.quality, self.preset,
                    chroma=self.chroma)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            done = set(written)
            finished = []
            with self.room:
                self.calls += 1
                if not written and status > 0 and not self.broken:
                    self.broken = True
                    self.log("WARNING: %s could not encode (exit status %d) - "
                             "the rest of this run is encoded on the CPU."
                             % (self.encoder, status))
                if self.broken:
                    self.cpu_queue.extend(self.pending)
                    self.pending, self.pending_pixels = [], 0
                for index, (book, entry) in enumerate(chunk):
                    if index in done:
                        self.pages += 1
                        self.pixels += entry["pixels"]
                        if self._page_done(book):
                            finished.append(book)
                    else:
                        self.fallbacks += 1
                        self.cpu_queue.appendleft((book, entry))
                self.running -= 1
                if self.running == 0:
                    self.busy += time.time() - self._busy_since
                self.room.notify_all()
            for book in finished:
                self._finished(book)

    # --- the CPU ------------------------------------------------------------
    def _next_for_cpu(self):
        """A page for a CPU encoder: its own first, then the last of the
        card's while the card has more than a chunk waiting without it. None
        once nothing is left that could come to the CPU."""
        with self.room:
            while True:
                if safety.abort_requested():
                    return None
                if self.cpu_queue:
                    return self.cpu_queue.popleft()
                if self.pending:
                    last = self.pending[-1][1]["pixels"]
                    if self.pending_pixels - last >= self.budget:
                        self.pending_pixels -= last
                        self.stolen += 1
                        self.room.notify_all()
                        return self.pending.pop()
                if (self.closing and not self.expected and not self.pending
                        and self.running == 0):
                    return None
                self.room.wait(_POLL_SECONDS)

    def _encode(self) -> None:
        while True:
            taken = self._next_for_cpu()
            if taken is None:
                break
            book, entry = taken
            subprocess.run(NICE + entry["cpu"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            with self.room:
                self.cpu_pages += 1
                self.cpu_pixels += entry["pixels"]
                last = self._page_done(book)
                self.room.notify_all()
            if last:
                self._finished(book)
        # Whatever an abort left waiting is not encoded, but its book still
        # has its folders to give back.
        with self.room:
            left = list(self.cpu_queue) + self.pending
            self.cpu_queue.clear()
            self.pending, self.pending_pixels = [], 0
            finished = [book for book, _entry in left if self._page_done(book)]
        for book in finished:
            self._finished(book)

    # --- books ----------------------------------------------------------------
    def _page_done(self, book: _Book) -> bool:
        """One page of <book> has an output; whether it was the last. Called
        under the lock."""
        book.outstanding -= 1
        return book.outstanding == 0

    def _finished(self, book: _Book) -> None:
        shutil.rmtree(book.folder, ignore_errors=True)

        self.finishers.submit(self.finish, book.item)
