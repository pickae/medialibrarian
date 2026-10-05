# File safety

Renaming never loses data and never writes outside the given input/output
folders:

- **No-op renames are skipped** for efficiency (a name already in its target form
  is left alone).
- **Clobbering renames are skipped** for safety: if the destination already exists
  as another file, the rename is *not* performed and the source is kept.
- Where both files are genuinely wanted (flattening nested folders, converting to
  a new format), a numbered suffix (`name (2).ext`) is used instead so **both**
  files survive.
- Each command **recaps the renames it skipped for safety, with their full
  `src -> dst` paths, at the end of the run.** This never interrupts execution — the
  run always finishes and simply reports what it deliberately left untouched, and a
  run stopped part-way still prints the recap for what it had done by then (see
  [Stopping a run](#stopping-a-run)).

## The input folder itself

Two rules every command that takes an input folder follows:

- **A folder you named is never deleted.** The commands prune folders their
  cleanup left empty, but the input (and output) folder you passed on the command
  line is still there afterwards — empty or not.
- **An input with nothing to do is explained, not worked through.** When the folder
  holds nothing the command can use, it names what it looked for and where, and exits
  non-zero, telling apart an *empty* folder (usually the wrong path) from one that
  *has content but none of it relevant* (usually the wrong tool, or files that still
  need an earlier step). The check runs before any de-duplication, renaming or output
  folder creation, so a refused run leaves everything as it was.

The name cleaners are the exception to the second rule by nature: any name at all
is work for them, so only a completely empty folder means there is nothing to do.

## How wide a run goes

The parallel commands default to a width taken from the logical CPU count, which
is the right guess for work that is CPU-bound and only that. Memory, disk
bandwidth and GPU memory are the limits that bite first on a smaller machine, and
none of them is visible in a core count — so on a host that swaps, thrashes or
runs out of VRAM, lower the width with the command's own `-j`/`-P` rather than
leaving it to the default.

## Stopping a run

Every long-running command can be stopped at any point — with `Ctrl+C`, with a
`kill`, or simply by closing the terminal window it is running in — and all three
end the run the same way:

- **Nothing that had not begun is begun.** The interrupt is recorded where every
  parallel worker can see it, so one `Ctrl+C` stops the whole run instead of only
  the file it landed on, and the run does not fall through into its next phase
  (re-concatenating, packaging, cube-building) over half-finished work.
- **The run still reports what it managed.** The closing report — the stats block,
  the counts, the safety recap — is printed for the part that got done, exactly as
  a finished run prints it. A run stopped four hours in still says what those four
  hours produced.
- **The RAM scratch is handed back.** All the intermediate work lives in a tmpfs
  (`/dev/shm`), so scratch left behind is memory that stays occupied until the
  machine is rebooted. Each run works in **one directory of its own** in there,
  named after the command, and releases that directory on the way out however the run
  ends — so concurrent runs never share a namespace, and anything left over names
  the run that abandoned it. Set `ramScratchBase` to put those directories on a
  different tmpfs.
- **The exit status says a person stopped it**, not that it went wrong: `130`
  (`128 + SIGINT`), so a caller or a cron job can tell the two apart.
- **A worker that dies is not a finished item.** A parallel run whose worker
  process ends abnormally — an uncaught error, a kill, a library that took the
  process down with it — names the item that was lost as it happens, recaps them
  at the end, and exits non-zero. The per-file failures a command handles itself
  are unchanged: those are reported and deliberately not fatal.

A second `Ctrl+C` during the wind-down is not caught, so an impatient user can
always get out — even when what is being waited for is a `rm -rf` over a mount that
has gone away.

Two runs deliberately do more than stop where they are: `content-census` still
writes the reports for the files it already read (but does **not** build cubes from
them, since a cube cannot say how complete the census behind it is), and `ytdlp`
finishes the download in flight before stopping.

## Pausing a run

Stopping is not the only way out of "I need this machine back". `convert-video`,
whose runs are the long ones, also takes `p` and `r` from the console: `p` stops every
video encoder of the moment where it is and `r` has them all carry on. The pause
reaches the encoders whatever the run is doing — a whole file, the chunks of one
spread across the CPU, or the engines of a GPU — because they are stopped by signal
rather than asked to stop, and it reaches the ones its parallel workers started as
readily as its own. What it frees is **computation, not memory**: a stopped encoder
still holds its RAM and VRAM, so this is for handing the cores or the GPU over for a
while, not for freeing them. It also lasts only as long as the run, and the run's
own report keeps the waiting out of its throughput figures. `Ctrl+C` still
works while paused — the encoders are continued first, so that a stopped one is never
left behind holding the memory the pause never released.

## The output folder must not sit inside the input

Every command that takes an **input and an output folder** — `convert-comics`,
`convert-images`, `convert-audio`, `convert-video`, `concat-audio`,
`convert-and-concat`, `ingest-books`, `ingest-music` — refuses an output
folder that *is* the input or lies anywhere inside it, before it creates or
renames anything.

What each of them writes to the output is the same kind of file it looks for in the
input (images → an image, a `.cbz` → a `.cbz`, audio → audio), so an output inside
the input hands the next run its own output to convert again — and the input cleanup
that runs first (de-duplication, empty-file and empty-folder pruning, renaming) would
reach into the finished library. Both paths are resolved first, so `<in>/../<in>/out`
and a symlink pointing back inside are caught too, while a *sibling* whose name
merely starts with the input's — the `<in>opus` convention `ingest-music` uses
for its default output — is not.

The reverse nesting is allowed: an input **inside** the output (`<library>` and
`<library>/incoming`) is a normal way to work and loses nothing.

## What leaves the machine

`ytdlp` downloads, so it obviously talks to the internet. Three other commands
do too, which is less obvious:

- **`ingest-music`** runs beets with the config that ships beside it, and that
  config enables `chroma`, `lastgenre`, `lyrics` and `fetchart`. So an ingest
  sends an acoustic fingerprint of each track to AcoustID, and asks MusicBrainz,
  Last.fm, a lyrics site and a cover-art host about the release. It also writes
  the tags it settles back **into your files** (`write: yes`). Edit
  `medialib/config/beets.yaml` if you would rather it did less.
- **`ingest-movies`** asks TheMovieDB what a film is, but only once `tmdbApiKey`
  is set — with no key it skips the lookup and says so. The key goes to curl on
  stdin rather than on its command line, so it is not readable from the process
  table. It downloads subtitles only from the catalogues whose login is set
  (OpenSubtitles.org, OpenSubtitles.com, SubDL); the logins reach the download
  helper through its environment, and SubDL's API key goes to SubDL in the
  request's address, which is where its API takes it.
- **`content-census-bi`** writes an `.html` page that loads the Perspective
  engine from jsDelivr when you open it, pinned to an exact release with the
  stylesheet checked against a hash. Your census data is not uploaded anywhere:
  it is embedded in the file, and the page's content-security policy allows no
  destination but that one origin. The engine's four scripts are ES modules,
  which have nowhere to carry a hash, so they are pinned by version and not by
  content — code jsDelivr serves runs in the page and can read the figures in
  it. Set `viewerCdnBase` to a folder holding that release to get a page that
  fetches nothing.
