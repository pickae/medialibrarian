"""Whether a convert-audio run cuts its long files into chunks, and which ones.

Cutting a file into one chunk per core is a trade. It wins the tail of the run:
one long book no longer pins a single core while the rest of the machine has
drained. It costs a planning pass that runs beside the queue's seed - a stream
copy of each file, one after another, and a silence search over nearly all of
its audio - holds the rest of the queue back until it is done, and leaves a join
for the end. Whether that pays depends on everything at once: how long the long
files are against the rest of the queue, how many of them there are against the
cores, and how much faster a job runs when the machine is not full - several
times faster for a lone job, which is what a whole-file tail is made of.

So the decision is made the way the run would unfold, by simulating its queue -
the seed, the planning beside it, the handover, the tail, the joins - and taking
the expected wall clock over what the files may turn out to hold. It is made one
question at a time rather than over every possible schedule:

1. Is the end of the run spent on the longest file alone? Encoded whole, it
   starts among the first jobs; if the rest of the queue, spread over the other
   workers, is still running when it finishes, the wall clock is not waiting on
   it, and nothing is cut. Only a run whose tail is that one file, with the
   rest of the machine drained, is weighed any further.
2. From which length? The cutoff starts at a default, is tried a step longer and
   a step shorter, and keeps moving in whichever direction shortens the expected
   wall clock until a step makes it longer. Moving it past the longest file is
   the decision not to chunk at all; moving it down to take in a few more files
   is what a tail of a few similar leftovers asks for.

The decision starts from what is known before a single probe: a file's byte size
and extension. The census of what real spoken-word trees hold - at
which rate, in which codec, with how many channels - turns bytes into the
seconds a file is likely to hold and what encoding them is likely to cost,
including the chance that it is under the re-encode threshold and costs next to
nothing. The costs were measured on one CPU; another is faster or slower at
every part of the trade alike, so only its thread count enters.

The census is a guess, and the files it picks to cut are the ones the guess
matters most for: the run's longest, which decide its wall clock either way. So
those, and only those, are probed for what they really hold, and the decision is
made again with their real length and rate. A file cut only once the guess is
corrected is probed in turn, so what is finally cut is never weighed by the
census alone. A run of a thousand files probes the few it may cut.
"""

import bisect
import heapq
import random
from dataclasses import dataclass

from medialib.lib import enums, fixedpoint, segments

__all__ = ["Decision", "decide", "census_for", "START_CUTOFF"]

# The cutoff the search starts from, in expected seconds of encoded audio.
START_CUTOFF = 10000.0
# Each step of the search moves the cutoff by this factor, and on as far as
# it takes to change which files are cut.
STEP = 1.25
# Two wall clocks this close are the same one, and a walk crosses at most this
# many such steps in a row before it stops.
TIE = 0.005
PLATEAU = 6
# The draws of what the files hold that the expectation is taken over - the
# same draws for every cutoff, so two cutoffs are compared on the same runs.
SCENARIOS = 12
# The files weighed one by one, in multiples of the worker pool: the largest
# by size, which is the queue's order, and the longest by expected length,
# which is where the candidates come from. The rest of a long queue - the
# smallest files, at its very end - is poured in as a stream of pieces, each
# this share of what is left per worker, down to the size of one of its files:
# coarse where the stream only keeps the machine busy, as fine as the files
# themselves where it makes the tail.
DISCRETE_FACTOR = 10
STREAM_SHARE = 0.25
# The queue's seed, in multiples of the worker pool (convert-audio's
# QUEUE_BUFFER_FACTOR): what encodes while the plan is made.
SEED_FACTOR = 2

# The census: what files of each extension hold, from probing a stratified
# sample of 15 000 files across two real spoken-word trees. Each row is
# (decoder, channels, kbps, share). A row's rate is the harmonic mean of its
# bucket - bytes become seconds as 1/rate - and the bucket edges sit on the 50
# and 90 kbps re-encode thresholds, so no row is both encoded and left alone.
CENSUS = {
    "mp3": (
        ("mp3", 1, 17, 0.050), ("mp3", 1, 29, 0.008), ("mp3", 1, 33, 0.116),
        ("mp3", 1, 48, 0.114), ("mp3", 1, 55, 0.024), ("mp3", 1, 65, 0.145),
        ("mp3", 1, 83, 0.020), ("mp3", 1, 96, 0.020), ("mp3", 1, 130, 0.028),
        ("mp3", 2, 33, 0.008), ("mp3", 2, 47, 0.010), ("mp3", 2, 57, 0.008),
        ("mp3", 2, 66, 0.102), ("mp3", 2, 82, 0.018), ("mp3", 2, 98, 0.045),
        ("mp3", 2, 119, 0.041), ("mp3", 2, 131, 0.193), ("mp3", 2, 169, 0.007),
        ("mp3", 2, 194, 0.011), ("mp3", 2, 326, 0.030),
    ),
    "m4b": (
        ("aac", 1, 32, 0.034), ("aac", 1, 67, 0.010), ("aac", 2, 33, 0.013),
        ("aac", 2, 63, 0.042), ("aac", 2, 69, 0.672), ("aac", 2, 84, 0.035),
        ("aac", 2, 99, 0.037), ("aac", 2, 126, 0.063), ("aac", 2, 131, 0.095),
    ),
    "m4a": (
        ("aac", 1, 66, 0.009), ("aac", 1, 96, 0.010), ("aac", 1, 124, 0.026),
        ("aac", 1, 133, 0.088), ("aac", 2, 48, 0.555), ("aac", 2, 55, 0.036),
        ("aac", 2, 67, 0.029), ("aac", 2, 101, 0.006), ("aac", 2, 126, 0.162),
        ("aac", 2, 132, 0.079),
    ),
    "mka": (
        ("aac", 2, 36, 0.007), ("aac", 2, 97, 0.014), ("aac", 2, 127, 0.043),
        ("opus", 1, 38, 0.048), ("opus", 1, 48, 0.008),
        ("opus", 1, 124, 0.059), ("opus", 1, 131, 0.022),
        ("opus", 2, 48, 0.342), ("opus", 2, 56, 0.297), ("opus", 2, 69, 0.084),
        ("opus", 2, 104, 0.007), ("opus", 2, 121, 0.009),
        ("vorbis", 1, 68, 0.013), ("vorbis", 2, 86, 0.008),
        ("vorbis", 2, 100, 0.034), ("vorbis", 2, 118, 0.007),
    ),
    "opus": (
        ("opus", 1, 31, 0.007), ("opus", 1, 36, 0.257), ("opus", 1, 47, 0.053),
        ("opus", 1, 54, 0.007), ("opus", 1, 74, 0.018),
        ("opus", 1, 104, 0.009), ("opus", 2, 34, 0.020),
        ("opus", 2, 48, 0.451), ("opus", 2, 57, 0.089), ("opus", 2, 68, 0.089),
    ),
    "ogg": (
        ("vorbis", 1, 30, 0.009), ("vorbis", 1, 34, 0.029),
        ("vorbis", 1, 45, 0.018), ("vorbis", 1, 59, 0.022),
        ("vorbis", 1, 75, 0.039), ("vorbis", 1, 84, 0.033),
        ("vorbis", 2, 76, 0.034), ("vorbis", 2, 86, 0.162),
        ("vorbis", 2, 100, 0.566), ("vorbis", 2, 117, 0.088),
    ),
    "flac": (
        ("flac", 1, 596, 0.060), ("flac", 2, 251, 0.006),
        ("flac", 2, 284, 0.934),
    ),
}
# Extensions that are another's under a different name.
CENSUS_ALIASES = {"aac": "m4a", "mpga": "mp3", "ogx": "ogg"}
# A container of video as well as audio: its bytes say little about the audio
# it holds, so it is weighed as a typical video file.
VIDEO_ROWS = (("aac", 2, 2000, 1.0),)
# An audio extension the census has no rows for, weighed as plain AAC.
UNKNOWN_ROWS = (("aac", 2, 128, 1.0),)

# What one second of audio costs to encode as Opus, in FULL-LOAD seconds - the
# seconds it takes a job while every thread of the machine is encoding - at
# the default output rate: 46 kbps keeping the source's channels, 32 kbps with
# -m. Keyed (decoder, source channels, forced mono). Measured on a 32-thread
# CPU (8 performance and 16 efficiency cores) with the sources in RAM.
OPUS_COST = {
    ("mp3", 2, False): 1 / 40.0,
    ("mp3", 1, False): 1 / 47.9,
    ("aac", 2, False): 1 / 40.6,
    ("aac", 1, False): 1 / 49.6,
    ("flac", 2, False): 1 / 29.9,
    ("vorbis", 2, False): 1 / 35.6,
    ("opus", 2, False): 1 / 35.6,
    ("mp3", 2, True): 1 / 47.1,
    ("aac", 2, True): 1 / 45.5,
    ("flac", 2, True): 1 / 48.4,
    ("vorbis", 2, True): 1 / 42.2,
    ("eac3", 6, True): 1 / 41.2,
}
# How the output rate moves that cost, by output channels: full-load seconds
# per audio second over the rate, from spoken word. Only the difference to the
# default rate is used. Opus is not smoothly cheaper at higher rates: it costs
# about the same from 16 kbps to the defaults, and then drops by a third where
# it leaves its speech modes for the transform codec alone, near 64 kbps.
RATE_COST = {
    2: ((16, 1 / 44.0), (24, 1 / 42.1), (32, 1 / 42.0), (40, 1 / 41.9),
        (46, 1 / 40.9), (64, 1 / 63.2), (96, 1 / 56.5), (128, 1 / 52.1)),
    1: ((16, 1 / 47.8), (24, 1 / 47.8), (32, 1 / 47.1), (46, 1 / 45.8),
        (64, 1 / 84.8)),
}
# xHE-AAC through exhale, against Opus at the same source and load.
XHEAAC_FACTOR = 3.5

# How much faster a job runs when fewer than all threads are busy, over the
# busy share of the machine. A lone job has a performance core, and its turbo,
# to itself.
SPEEDUP = ((1 / 32, 2.81), (2 / 32, 2.45), (4 / 32, 2.31), (0.25, 1.98),
           (0.5, 1.58), (0.75, 1.24), (1.0, 1.0))

# How many times a decision is made again, each time with the files it cut
# probed: it settles in one or two, and the bound only keeps a probe that keeps
# changing its mind from probing the whole tree.
PROBE_ROUNDS = 4

# A job's fixed cost whatever its length - the probes around the encode, its
# scratch directory, the cover - and what a file that is left alone costs, in
# full-load seconds.
JOB_OVERHEAD = 0.15
SKIP_COST = 0.10

# The planning, per second of a candidate's audio. The stream copy is one
# process per file, a few files at once (segments.SEEK_COPY_JOBS), at the pace
# of its own demuxing - several times slower while every thread is also
# encoding - or of the disk they share, whichever is slower. A format that
# seeks cheaply as it is (enums.SEEK_CHEAP_EXTENSIONS) is not copied at all.
SEEK_IDLE = 1.3e-4
SEEK_LOADED = 6.0e-4
DISK_BYTES_PER_SECOND = 120e6
# The silence search: seconds one worker spends decoding one second of audio,
# on an idle machine; and how much longer it takes beside a full queue. FLAC
# is from a half-hour file only, and E-AC-3 was not measured at all - it is
# rare enough in the census that a guess between the others does.
DETECT_COST = {"mp3": 4.0e-3, "aac": 3.4e-3, "vorbis": 4.2e-3,
               "opus": 9.0e-3, "flac": 1.9e-3, "eac3": 6.0e-3}
DETECT_LOADED_FACTOR = 1.9
# What the planning leaves the encodes beside it of their pace: next to nothing
# is taken by the one stream copy, most of it by a pool as wide as theirs.
SEEK_SLOWDOWN = 0.97
DETECT_SLOWDOWN = 0.4
# What a chunk pays that a whole file does not: opening the source mid-way,
# twice - to encode its range, and to measure it - which parses the source's
# whole index each time, so it grows with the source's length.
CHUNK_OPEN_PER_SECOND = 1.5e-5
# The join after the queue has drained: a stream copy of the output, and the
# tags and cover written back into it.
JOIN_FIXED = 0.5
JOIN_PER_SECOND = 2.0e-4


@dataclass(frozen=True)
class Decision:
    """Which tracks to cut, by their position in the list weighed - empty for
    "cut nothing" - and the expected wall clocks, in seconds, that decided it:
    with that choice, and with every file encoded whole."""

    chunked: frozenset
    expected: float
    whole: float


def census_for(extension):
    """The census rows for one extension, or () when it has none."""
    return CENSUS.get(CENSUS_ALIASES.get(extension, extension), ())


def _read(table, value):
    """A piecewise-linear read of ((x, y), ...), flat beyond its ends."""
    xs = [x for x, _ in table]
    i = bisect.bisect_left(xs, value)
    if i <= 0:
        return table[0][1]
    if i >= len(table):
        return table[-1][1]
    (x0, y0), (x1, y1) = table[i - 1], table[i]
    return y0 + (y1 - y0) * (value - x0) / (x1 - x0)


def cost_per_second(decoder, channels, mono, bitrate, codec="opus"):
    """Full-load seconds one second of this audio takes to encode."""
    if mono:
        base = OPUS_COST.get((decoder, channels, True))
        if base is None:
            # A mono source downmixed is a mono source: it decodes as one.
            base = OPUS_COST.get((decoder, 1, False), OPUS_COST[("aac", 1,
                                                                 False)])
        out, default = 1, 32
    else:
        # A surround source is downmixed to stereo unless -u keeps it, and
        # is rare enough in the census that its cost is taken as stereo's.
        base = OPUS_COST.get((decoder, min(channels, 2), False),
                             OPUS_COST[("aac", min(channels, 2), False)])
        out, default = min(channels, 2), 46
    table = RATE_COST[out]
    cost = base + _read(table, bitrate) - _read(table, default)
    return cost * XHEAAC_FACTOR if codec == "xheaac" else cost


def _speed(busy, threads):
    return _read(SPEEDUP, busy / float(threads))


class _Kind:
    """What the files of one extension may turn out to hold, per byte.

    One object per extension rather than per file: a run of a hundred
    thousand tracks has a handful of extensions, and the census is the same
    for every file of one.
    """

    def __init__(self, rows, video, always, mono, bitrate, threshold, codec,
                 copied=True, seconds_per_byte=None, verdict=None):
        # Whether the planner stream-copies a candidate of this kind first.
        self.copied = copied
        # Per row: (share, encoded seconds, encode cost, search cost) - the
        # last three per byte, and zero when the row is left alone.
        self.rows = []
        self.cumulative = []
        total = 0.0
        for decoder, channels, kbps, share in rows:
            # A probed file's length is what it holds, not what its rate
            # would make of its bytes: its container has cover art and, in a
            # video, a picture beside the audio.
            per_byte = seconds_per_byte if seconds_per_byte is not None \
                else 8.0 / (kbps * 1000.0)
            # A folder's verdict overrules its files' own rates, but not a
            # video or a format that is converted whatever its rate.
            if video or always or (kbps * 1000 >= threshold
                                   if verdict is None else verdict):
                self.rows.append((
                    share, per_byte,
                    per_byte * cost_per_second(decoder, channels, mono,
                                               bitrate, codec),
                    per_byte * DETECT_COST.get(decoder, DETECT_COST["aac"])))
            else:
                self.rows.append((share, 0.0, 0.0, 0.0))
            total += share
            self.cumulative.append(total)
        self.seconds = sum(r[0] * r[1] for r in self.rows) / total
        self.cost = sum(r[0] * r[2] for r in self.rows) / total
        encoded = sum(r[0] for r in self.rows if r[1] > 0) / total
        self.fixed = encoded * JOB_OVERHEAD + (1 - encoded) * SKIP_COST

    def draw(self, rng):
        at = rng.random() * self.cumulative[-1]
        return self.rows[min(bisect.bisect_left(self.cumulative, at),
                             len(self.rows) - 1)]


def _stream_pieces(work, count, workers):
    """<work> seconds of <count> small files, as the pieces they are poured in
    as: each a share of what is left, and none smaller than one file."""
    if count <= 0 or work <= 0:
        return []
    smallest = work / count
    pieces = []
    while work > smallest * 0.5:
        piece = max(smallest, work * STREAM_SHARE / workers)
        pieces.append(min(piece, work))
        work -= piece
    return pieces


def _makespan(seed, release, slow, tail, workers, threads, watch=None):
    """The wall clock of one queue, from its first job to its last - or, with
    <watch> a position in <seed>, the pair (when that job finishes, when every
    other job has).

    <seed> is there at once, <tail> only from <release> on - the moment the plan
    is ready - and <slow> is ``[(until, pace), ...]``: how much of their pace the
    planning leaves the encodes, up to each point in time. Every running job
    progresses at the pace the number of busy workers gives, so the simulation
    keeps one clock of progress and each job its finishing point on it.
    """
    now = progress = 0.0
    finishing = []
    queue = seed[::-1]
    pending = tail
    edges = [until for until, _pace in slow if until > 0]
    watched, watched_end, rest_end, started = None, 0.0, 0.0, 0
    while True:
        while queue and len(finishing) < workers:
            end = progress + queue.pop()
            if started == watch:
                watched = end
            heapq.heappush(finishing, end)
            started += 1
        if not finishing:
            if pending is None:
                return now if watch is None else (watched_end, rest_end)
            now = max(now, release)
            queue, pending = pending[::-1], None
            continue
        pace = _speed(len(finishing), threads)
        for until, factor in slow:
            if now < until:
                pace *= factor
                break
        finish = now + (finishing[0] - progress) / pace
        edge = next((e for e in edges if e > now), None) \
            if pending is not None else None
        if edge is not None and edge < finish:
            progress += (edge - now) * pace
            now = edge
            if now >= release:
                queue = pending[::-1] + queue
                pending = None
            continue
        progress = finishing[0]
        now = finish
        while finishing and finishing[0] <= progress + 1e-9:
            if heapq.heappop(finishing) == watched:
                watched, watched_end = None, now
            else:
                rest_end = now


class _Run:
    """The queue a run would make, and what it is expected to take."""

    def __init__(self, sizes, kinds, workers, threads):
        self.workers, self.threads = workers, threads
        self.sizes, self.kinds = sizes, kinds
        count = len(sizes)
        order = sorted(range(count), key=lambda i: -sizes[i])
        self.seconds = [sizes[i] * kinds[i].seconds for i in range(count)]
        limit = DISCRETE_FACTOR * workers
        longest = sorted(range(count), key=lambda i: -self.seconds[i])
        keep = set(order[:limit]) | set(longest[:limit])
        # Weighed one by one, in the queue's order: largest file first.
        self.discrete = [i for i in order if i in keep]
        rest = [i for i in order if i not in keep]
        stream = sum(sizes[i] * kinds[i].cost + kinds[i].fixed for i in rest)
        self.stream = _stream_pieces(stream, len(rest), workers)
        rng = random.Random(1)
        self.draws = [[kinds[i].draw(rng) for i in self.discrete]
                      for _ in range(SCENARIOS)]

    def ends_on_longest(self):
        """Whether the queue encoded whole is, at its end, waiting on its
        longest file alone: that file finishing, expected over the draws,
        no sooner than every other job has - a twin of it finishing with it
        is a tail of long files all the same."""
        longest = rest = 0.0
        for draw in self.draws:
            jobs = [self.sizes[i] * cost + JOB_OVERHEAD if cost > 0
                    else SKIP_COST
                    for i, (_share, _seconds, cost, _search)
                    in zip(self.discrete, draw, strict=True)]
            watch = max(range(len(jobs)), key=jobs.__getitem__)
            ends = _makespan(jobs + self.stream, 0.0, (), None, self.workers,
                             self.threads, watch)
            longest, rest = longest + ends[0], rest + ends[1]
        return longest >= rest * (1 - 1e-9)

    def chunked_at(self, cutoff):
        return frozenset(i for i in self.discrete if self.seconds[i] > cutoff)

    def expected(self, chunked):
        return sum(self._one(chunked, draw)
                   for draw in self.draws) / len(self.draws)

    def _one(self, chunked, draw):
        workers, threads = self.workers, self.threads
        whole, pieces, candidates = [], [], []
        for slot, i in enumerate(self.discrete):
            size = self.sizes[i]
            _share, seconds, cost, search = draw[slot]
            if cost <= 0:
                whole.append((size, SKIP_COST))
            elif i in chunked:
                candidates.append((size, size * seconds, size * search,
                                   self.kinds[i].copied))
                pieces += [(size / workers, size * cost / workers
                            + JOB_OVERHEAD
                            + CHUNK_OPEN_PER_SECOND * size * seconds)] \
                    * workers
            else:
                whole.append((size, size * cost + JOB_OVERHEAD))
        if not chunked:
            jobs = [cost for _size, cost in whole] + self.stream
            return _makespan(jobs, 0.0, (), None, workers, threads)

        # The seed is the largest of what needs no plan, and it is all that
        # encodes until the plan is ready. How busy it keeps the machine
        # decides how fast the planning beside it runs.
        room = SEED_FACTOR * workers
        seed = [cost for _size, cost in whole[:room]]
        tail = whole[room:] + pieces
        tail.sort(key=lambda job: -job[0])
        seed_work = sum(seed)
        busy = 1.0
        for _pass in range(2):
            pace = SEEK_IDLE + (SEEK_LOADED - SEEK_IDLE) * busy
            copies = [seconds * pace for _size, seconds, _search, copied
                      in candidates if copied]
            copied_bytes = sum(size for size, _seconds, _search, copied
                               in candidates if copied)
            seek = max(copied_bytes / DISK_BYTES_PER_SECOND,
                       max(copies, default=0.0),
                       sum(copies) / max(1, min(segments.SEEK_COPY_JOBS,
                                                workers)))
            detect = sum(c[2] for c in candidates) / workers \
                * (1 + (DETECT_LOADED_FACTOR - 1) * busy)
            busy = min(1.0, seed_work / (workers * max(1e-9, seek + detect)))
        release = seek + detect
        wall = _makespan(seed, release,
                         ((seek, SEEK_SLOWDOWN), (release, DETECT_SLOWDOWN)),
                         [cost for _size, cost in tail] + self.stream,
                         workers, threads)
        if candidates:
            joins = sorted((JOIN_FIXED + JOIN_PER_SECOND * seconds
                            for _size, seconds, _search, _copied
                            in candidates),
                           reverse=True)
            wall += max(joins[0], sum(joins) / workers) / _speed(
                min(len(joins), workers), threads)
        return wall


def decide(tracks, workers, threads, mono, bitrate, threshold, is_video,
           always_transcode, codec="opus", probe=None, verdicts=None):
    """The run's chunking: which of <tracks> to cut, if any.

    <tracks> is ``[(extension, size), ...]`` in any order. <workers> is the
    encode pool and <threads> the machine's. <bitrate> is the output rate in
    kbps and <threshold> the source rate, in bps, from which a file is
    re-encoded rather than left alone; <is_video> and <always_transcode> say
    of an extension whether it is encoded whatever its rate.

    <verdicts>, beside <tracks>, is what convert-audio -g judged of each
    track's folder: True for converted whole, False for kept whole, None where
    the track's own rate decides.

    <probe>, given a track's position, answers ``(decoder, channels, bps,
    seconds)`` for its first audio stream, or None when it cannot say. It is
    asked only of the tracks a decision would cut, and each at most once.
    """
    if workers < 2 or not tracks:
        return Decision(frozenset(), 0.0, 0.0)
    verdicts = verdicts or [None] * len(tracks)
    known = {}
    census = []
    for (extension, _size), verdict in zip(tracks, verdicts):
        if (extension, verdict) not in known:
            video = is_video(extension)
            rows = VIDEO_ROWS if video else (census_for(extension)
                                             or UNKNOWN_ROWS)
            known[extension, verdict] = _Kind(
                rows, video, always_transcode(extension), mono, bitrate,
                threshold, codec,
                extension not in enums.SEEK_CHEAP_EXTENSIONS,
                verdict=verdict)
        census.append(known[extension, verdict])
    kinds = list(census)
    asked = set()
    decision = _decide(tracks, kinds, workers, threads)

    def probe_round():
        """The files the decision cuts that are not yet probed, probed, and
        the decision made again; whether there were any."""
        nonlocal decision
        fresh = sorted(decision.chunked - asked)
        if not fresh:
            return False
        for index in fresh:
            asked.add(index)
            found = probe(index)
            if found is None:
                continue
            decoder, channels, bps, seconds = found
            extension, size = tracks[index]
            if size <= 0 or seconds <= 0:
                continue
            # The rate the threshold is read against is the audio stream's
            # own; one that states none is taken as its bytes over its length.
            kbps = (bps or size * 8.0 / seconds) / 1000.0
            kinds[index] = _Kind(
                ((decoder, channels, kbps, 1.0),), is_video(extension),
                always_transcode(extension), mono, bitrate, threshold, codec,
                census[index].copied, seconds / size, verdicts[index])
        decision = _decide(tracks, kinds, workers, threads)
        return True

    if probe:
        fixedpoint.until_stable(probe_round, PROBE_ROUNDS)
    return decision


def _decide(tracks, kinds, workers, threads):
    """One decision, with each track weighed as its kind says."""
    run = _Run([size for _extension, size in tracks], kinds, workers,
               threads)
    whole = run.expected(frozenset())

    # 1. The longest file starts among the first jobs. If the rest of the
    #    queue is still running when it finishes, the end of the run is not
    #    waiting on it, and there is nothing more to weigh.
    if not run.ends_on_longest():
        return Decision(frozenset(), whole, whole)

    # 2. The cutoff, a step at a time in whichever direction shortens the
    #    expected wall clock, until a step no longer does.
    # The range a cutoff can make a difference in: between the shortest file
    # that holds anything to encode and the longest. An empty file, or one
    # sure to be left alone, holds nothing any cutoff could fall below.
    lengths = [run.seconds[i] for i in run.discrete if run.seconds[i] > 0]
    if not lengths:
        return Decision(frozenset(), whole, whole)
    longest, shortest = max(lengths), min(lengths)
    seen = {frozenset(): whole}

    def value(chunked):
        if chunked not in seen:
            seen[chunked] = run.expected(chunked)
        return seen[chunked]

    def step(cutoff, factor):
        """The next cutoff this way that changes what is cut, or None once
        nothing further this way could."""
        chunked = run.chunked_at(cutoff)
        moved = cutoff
        while (moved >= shortest / STEP) if factor < 1 else (
                moved <= longest * STEP):
            moved *= factor
            if run.chunked_at(moved) != chunked:
                return moved
        return None

    def walk(cutoff, best, factor):
        """From <cutoff> this way for as long as the wall clock does not get
        longer: the best cutoff met, and its wall clock.

        A step that changes next to nothing - files that in no draw turn out
        to need encoding, so cutting them costs a probe and saves nothing - is
        walked across rather than taken as the end, for up to PLATEAU steps.
        """
        found, flat = (cutoff, best), 0
        while True:
            cutoff = step(cutoff, factor)
            if cutoff is None:
                return found
            there = value(run.chunked_at(cutoff))
            if there < found[1] * (1 - TIE):
                found, flat = (cutoff, there), 0
            elif there <= found[1] * (1 + TIE) and flat < PLATEAU:
                flat += 1
            else:
                return found

    start = value(run.chunked_at(START_CUTOFF))
    cutoff, best = min([walk(START_CUTOFF, start, STEP),
                        walk(START_CUTOFF, start, 1 / STEP)],
                       key=lambda found: found[1])
    chunked = run.chunked_at(cutoff)
    if not chunked or best >= whole:
        return Decision(frozenset(), whole, whole)
    return Decision(chunked, best, whole)
