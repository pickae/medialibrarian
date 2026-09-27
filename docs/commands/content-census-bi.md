# `content-census-bi`

Turns the census reports into a queryable DuckDB database of pre-aggregated
hypercubes **and a single self-contained `.html` page to explore them in**: the
census says what is in a library file by file, this says **how much of it is
what**. How many hours of 2160p, how many gigabytes of comics scanned below
1080p, what the duration-weighted average bitrate of the Opus audiobooks is —
without anyone writing a `GROUP BY`. Give it the reports, or the folder they are
in (it looks recursively).

Both halves, always. A cube nobody can look at answers no question, so there is
no separate frontend to forget to run. It reads reports, never media, so
it is the **cheap** half and stands on its own: run it over reports that already
exist and no library is walked again. Nothing is opened or renamed either way.

Per content type it builds the report as a typed table, a fact view over it with
every dimension already bucketed (resolution tiers, aspect ratio, HFR, SDR/HDR/DV,
starved/adequate/generous) and never null, and the hypercube itself. The fact view
is the drill-through — it still holds the paths, so "which files are those" stays
one SQL query away.

**Resolution and aspect ratio are two independent readings of one column.** The
census records the coded pixel size a probe reported; the tier says how much
detail is in it (`SD` … `4320p`), and the aspect ratio bucket says what shape it
is (`1.33:1`, `1.78:1`, `2.39:1`, `0.56:1` …), because a scope film and a vertical
short can both be 1080p. Shape is shown as the ratio normed to a height of 1 — the
one of a bucket's three names that can be compared at a glance — and the integer
ratio (`16:9`, `239:100`) and the marketing names (Scope, Academy, Univisium) sit
next to it. It is an axis of the page rather than of the cube: 22 buckets would double the largest cube for
a question the pivot table answers from the base grain for free.

**The codec is read three ways for the same reason.** What the file states
(`h264`, `hevc`, `msmpeg4v3`) is a fact and stays an axis of its own; beside it the
page can group by the codec's **family**, which is that codec under every spelling
it arrives in, and by its **generation** — MPEG-2 era, MPEG-4 ASP era, or everything
from H.264 on. The last is the one worth asking a whole library: it is the answer to
"how much of this is old enough to be worth re-encoding", and it comes from the
same table `convert-video -t` judges a single file with. Both are page axes, and free ones — each is a function of a column the cube already carries.

**Bitrate adequacy is a cube axis, and the one that pays for itself.** The census
has already decided, per film, whether its video bitrate is starved, adequate or
generous *for what that file is*; here it is only an axis like any other, so "how
many terabytes of the 2160p HEVC in this library is generous" — the re-encode queue,
in one drill-down — is a filter rather than a query. It is the one axis worth a
doubling of the cube's grouping sets, where the aspect ratio and the bitrate bands
were left to the page for exactly that cost: those slice a library, this one names
the part of it that is worth acting on.

The page is one file with one tab per content type, each a
[Perspective](https://perspective-dev.github.io/) pivot table. Opening it needs
**nothing installed**: no server, no DuckDB, no Python. The data is embedded in
the file and the page sends it nowhere: it carries a content-security policy
that denies every destination except the one the engine is fetched from, so the
page needs the network the *first* time it is opened on a machine and nothing
after that. The engine itself is the other side of that: it is code from
jsDelivr, running in the page beside your figures, pinned to an exact release.
Point `viewerCdnBase` at a local copy of that release and the page reaches
nothing at all.

**The page reads in human units**: sizes in gigabytes (1 GB = 1,000,000,000
bytes) and durations as `hours:minutes`. Only the page — the reports, the fact
views and the cubes keep the exact bytes and seconds a probe reported. Both are a
multiplication by a constant, so every roll-up still adds up.

The database is rebuilt from nothing on every run and says so — a cube is derived
data with no history in it. The new one is built beside the old and moved onto it
at the end, so a run that is refused, interrupted or that DuckDB rejects leaves
the previous database exactly as it was.
