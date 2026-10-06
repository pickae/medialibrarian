# `content-census-bi`

Turns the [census](content-census.md) reports into a queryable DuckDB database of
pre-aggregated hypercubes **and a single self-contained `.html` page to explore
them in**: the census says what is in a library file by file, this says **how
much of it is what**.

    content-census-bi [options] <reportPath>...

| At a glance | |
| --- | --- |
| **Takes** | census reports, or the folder they are in (it looks recursively) |
| **Writes** | `contentCensusBI.duckdb` and `contentCensusBI.html` beside the first report (`-o` names another) |
| **Input** | reads reports, never media: nothing is opened or renamed |
| **Reruns** | rebuilt from nothing every time, and says so — a cube is derived data with no history in it |
| **Network** | none while building; the page fetches its engine the first time it is opened ([below](#the-page)) |

## Default behavior

With no options, a run:

- looks for census reports recursively in any folder given
- **rebuilds the database from nothing**, replacing the previous one only once the new one is complete
- writes the `.duckdb` and the `.html` page beside the first audio, video, images, books or comics report, in that order (`-o`)
- exports no CSV, prints no totals and runs no query (`-e`, `-s`, `-c`)

How many hours of 2160p, how many gigabytes of comics scanned below 1080p, what
the duration-weighted average bitrate of the Opus audiobooks is — without anyone
writing a `GROUP BY`.

Both halves, always. A cube nobody can look at answers no question, so there is
no separate frontend to forget to run. It reads reports, never media, so it is
the **cheap** half and stands on its own: run it over reports that already exist
and no library is walked again.

## What it builds

Per content type:

| Object | What it is |
| --- | --- |
| the report, as a typed table | what the census wrote |
| a fact view over it | every dimension already bucketed (resolution tiers, aspect ratio, HFR, SDR/HDR/DV, starved/adequate/generous) and never null. It is the drill-through — it still holds the paths, so "which files are those" stays one SQL query away |
| the hypercube itself | the roll-ups the page reads |

The new database is built beside the old and moved onto it at the end, so a run
that is refused, interrupted or that DuckDB rejects leaves the previous database
exactly as it was.

## The axes

Some columns are read more than one way. Each reading is either an axis of the
cube, or an axis of the page only — the page computes those from the base grain
for free, where a cube axis doubles the grouping sets.

| Axis | Read from | Where |
| --- | --- | --- |
| resolution tier (`SD` … `4320p`) | the coded pixel size | cube |
| aspect ratio (`1.33:1`, `1.78:1`, `2.39:1`, `0.56:1` …) | the same pixel size | page |
| codec as stated (`h264`, `hevc`, `msmpeg4v3`) | the codec | cube |
| codec family | the same codec | page |
| codec generation | the same codec | page |
| bitrate adequacy (starved / adequate / generous) | the census's verdict (`content-census -a`) | cube |

**Resolution and aspect ratio are two independent readings of one column.** The
census records the coded pixel size a probe reported; the tier says how much
detail is in it, and the aspect ratio bucket says what shape it is, because a
scope film and a vertical short can both be 1080p. Shape is shown as the ratio
normed to a height of 1 — the one of a bucket's three names that can be compared
at a glance — and the integer ratio (`16:9`, `239:100`) and the marketing names
(Scope, Academy, Univisium) sit next to it. It is an axis of the page rather than
of the cube: 22 buckets would double the largest cube for a question the pivot
table answers from the base grain for free.

**The codec is read three ways for the same reason.** What the file states is a
fact and stays an axis of its own; beside it the page can group by the codec's
**family**, which is that codec under every spelling it arrives in, and by its
**generation** — MPEG-2 era, MPEG-4 ASP era, or everything from H.264 on. The
last is the one worth asking a whole library: it is the answer to "how much of
this is old enough to be worth re-encoding", and it comes from the same table
`convert-video -t` judges a single file with.

**Bitrate adequacy is a cube axis, and the one that pays for itself.** The census
has already decided, per film, whether its video bitrate is starved, adequate or
generous *for what that file is*; here it is only an axis like any other, so "how
many terabytes of the 2160p HEVC in this library is generous" — the re-encode queue,
in one drill-down — is a filter rather than a query. It is the one axis worth a
doubling of the cube's grouping sets, where the aspect ratio and the bitrate bands
were left to the page for exactly that cost: those slice a library, this one names
the part of it that is worth acting on.

## The page

The page is one file with one tab per content type, each a
[Perspective](https://perspective-dev.github.io/) pivot table. Opening it needs
**nothing installed**: no server, no DuckDB, no Python.

- **The data stays in the file.** It is embedded, and the page sends it nowhere:
  it carries a content-security policy that denies every destination except the
  one the engine is fetched from, so the page needs the network the *first* time
  it is opened on a machine and nothing after that.
- **The engine is code from jsDelivr**, running in the page beside your figures,
  pinned to an exact release. Point `viewerCdnBase` at a local copy of that
  release and the page reaches nothing at all.
- **The page reads in human units**: sizes in gigabytes (1 GB = 1,000,000,000
  bytes) and durations as `hours:minutes`. Only the page — the reports, the fact
  views and the cubes keep the exact bytes and seconds a probe reported. Both are
  a multiplication by a constant, so every roll-up still adds up.

---

**Options:** `content-census-bi -h` lists every option and its default.
**Shared rules:** [file safety](../file-safety.md) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[what leaves the machine](../file-safety.md#what-leaves-the-machine) ·
[what a run prints](../output.md)
