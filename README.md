# Media management commands

Command-line tools for ingesting, transcoding and tidying up personal media
libraries — audiobooks, music, movies, comics and image galleries. Every one that
renames files follows the same "never clobber, never lose a file" rules,
described under [File safety](#file-safety).

Each is an installed command. From a checkout, `pip install .` puts all eighteen
on your PATH; `pip install -e .` does the same and keeps them running the
checkout, which is what you want while editing it. Under a plain `pip install .`
the per-machine files — the `data/` tables, the `logs/` logs — live under
`~/.local/share/medialib` (or `$XDG_DATA_HOME/medialib` if it is set); the
environment variable `CLI_SCRIPT_DIR` points them somewhere else.

Heavy intermediate work is kept in RAM (`/dev/shm` / tmpfs) wherever possible, so
only final outputs are written back to disk. Each run gets a scratch directory of
its own in there, so several can run at once without sharing one.

> **Platform:** these target **Linux**, and **macOS** is supported on a
> best-effort basis — everything is written to work there, but no macOS machine
> runs the suite, so treat it as untested rather than as broken (see
> [Running on a Mac](#running-on-a-mac)). On a Windows machine **WSL2** is the
> way to run them. Native Windows is **experimental for now**: the tool-free
> cases import and run there, but the commands that drive the media tools and
> POSIX coreutils expect a POSIX host. Neither Windows nor macOS has a
> RAM-backed filesystem, so the scratch above lands in `%TEMP%` / `$TMPDIR` on a
> normal disk; a RAM disk mounted by hand works if `ramScratchBase` points at
> it.

## At a glance

| Command | Does |
| --- | --- |
| [`concat-audio`](#concat-audio) | One audiobook file per input subfolder, with chapters |
| [`convert-audio`](#convert-audio) | Spoken-word audio → low-bitrate Opus |
| [`convert-and-concat`](#convert-and-concat) | The two above chained, intermediate tree in RAM |
| [`transcribe-audio`](#transcribe-audio) | Audio and video → whisper transcripts in a mirrored folder |
| [`convert-video`](#convert-video) | Re-encode a video library (AV1/x265 + Opus) |
| [`ingest-movies`](#ingest-movies) | Sort, clean and improve a movie library in place |
| [`ingest-music`](#ingest-music) | Downloads → clean lossless FLAC library |
| [`ingest-books`](#ingest-books) | E-books → clean, uniform epub/pdf library |
| [`read-library`](#read-library) | E-books → audiobooks (Opus + lossless), read aloud by a TTS engine |
| [`convert-comics`](#convert-comics) | `.cbr`/`.cbz`/`.cb7` and comic PDFs → `.cbz` of AVIF pages |
| [`convert-images`](#convert-images) | Batch image → AVIF (or back to JPEG) |
| [`clean-folder-structure`](#clean-folder-structure) | Apply the shared name cleaners across a tree |
| [`find-fragment-candidates`](#find-fragment-candidates) | Report the recurring name fragments a library still carries |
| [`ytdlp`](#ytdlp) | Download tables of podcast feeds as audio or video, Windows or Linux |
| [`cue-to-chapters`](#cue-to-chapters) | A `.cue` sheet → an OGM chapter file |
| [`content-census`](#content-census) | Census one or more libraries into one CSV per content type |
| [`content-census-bi`](#content-census-bi) | Roll those reports up into DuckDB hypercubes and a pivot-table page |

**Every command prints its arguments, options and defaults with `-h`** (or
`--help`), which needs none of the media tools installed — so this file is about
what the commands do and the rules they all follow, not about their flags.
`cue-to-chapters` is the one exception: it takes two file names and no options,
so any argument list but those two prints its usage and fails.

Every option has both forms: `-j 8` and `--jobs 8` (or `--jobs=8`) are the same
option. Abbreviations are not accepted — `--job` is not `--jobs` — so a new
option can never change what a command you already type means.

## Requirements

- Python 3.11+, and one package with it: `mutagen`, which the install brings in
- A UTF-8 locale (accented/multibyte filenames are handled character-wise)
- Per-tool external dependencies (see each command's `-h` output). Across the set:
  `ffmpeg`, `mkvtoolnix`, `dovi_tool`, `mediainfo`, `ImageMagick`, `rsync`,
  `yt-dlp`, `fdupes`, `unrar`/`unzip`/`7z`/`tar`/`zstd`, `zip`, poppler-utils (`pdftoppm`,
  `pdfinfo`, `pdfimages`, `pdftotext`),
  `whisper-ctranslate2`, `ffsubsync`, `tree`, `beets`, Calibre's `ebook-convert`,
  Ghostscript (`gs`), `duckdb` and `wc` — that last one being the only piece of
  coreutils anything here still shells out to.
- `read-library` additionally needs a local
  [ebook2audiobook](https://github.com/DrewThomasson/ebook2audiobook) checkout and
  a Python 3.10–3.12 to build its environment from; everything inside that
  environment is installed by the checkout itself (see
  [`read-library`](#read-library)).

### Missing tools are refused up front

Each command checks the external tools it is about to drive **before it touches
anything**, and refuses the run naming *every* missing one at once, what it is
needed for and how to install it. The list is **what this run needs**, not
everything the command can ever use: `ingest-books -t` asks for Calibre alone,
and `convert-comics` only asks for `unrar` when the input actually holds a
`.cbr`.

### …unless the run can do its job anyway

Only a tool whose absence would spoil the output, or silently drop part of it, is
worth refusing over. The rest say what they cannot do and the run goes on:

| Missing | What happens instead |
| --- | --- |
| `dovi_tool` (or `mkvmerge`) | the Dolby Vision file is left exactly as it came in, and a dual-layer profile 7 source is re-encoded as plain HDR10 |
| poppler (`pdfinfo`/`pdfimages`/`pdftoppm`) | the PDFs that therefore cannot be inspected are reported |
| `nvidia-smi` | commentary is transcribed, and books are narrated, on the CPU |
| `flock` | progress is printed one line per item, without its `[n of total]` position |
| `wc` | the books census leaves its word and character columns blank, and `read-library` reads in name order instead of longest-first |
| Calibre's `ebook-convert` | a book whose metadata does not state its language is narrated in the engine's default, and the reading queue is ordered by file size rather than by length |
| `ffsubsync` **or** `pipx` | **both** subtitle producers are skipped together |
| `AtomicParsley` | the podcast episodes that arrive as m4a get no cover art |

Subtitles are all-or-nothing on purpose: a downloaded subtitle is usually cut for
a different release and a whisper transcript is only as trustworthy as the
alignment that proves it matches the audio, so neither is worth muxing in
unaligned. `ffsubsync` is what decides that and `pipx` is what runs the two
producers, so missing either skips subtitle downloading *and* commentary
transcription together, and everything else runs normally.

### Running on a Mac

Everything here is written to work on macOS and nothing is written *only* for
it: the differences are handled where they are, and each one is a fallback the
same code takes on any host that needs it. **No Mac runs the test suite**, so
this is best-effort — report anything that does not hold.

Homebrew is where the tools come from, and a refusal names the `brew` command
rather than the `apt` one. Two formulae are not named after the binary they
install: 7-Zip is `brew install sevenzip` (and the binary is `7zz`, which the
`7z|7zz|7za` alternatives already accept), and `unrar` was dropped from
homebrew-core over its licence, so it comes from a tap. `curl`, `zip`, `unzip`
and `tar` ship with the system.

What differs, and what happens about it:

| On a Mac | What the run does |
| --- | --- |
| no `/dev/shm`, no tmpfs | scratch lands in `$TMPDIR`, on disk; a RAM disk made with `hdiutil`/`diskutil` is used by pointing `ramScratchBase` at it |
| no `~/.cache` | the disk spill goes under `~/Library/Caches` (`$XDG_CACHE_HOME` still wins) |
| no `flock(1)` | progress lines keep their `[n of total]` position anyway — the lock is the C library's, which macOS has |
| no `/dev/dri`, no NVENC | hardware **decode** goes through VideoToolbox; **encoding** is the software profiles (`av1Svt`, `x265`) — the `*Nvenc` profiles need an NVIDIA card and refuse up front |
| `iconv` is GNU libiconv, which spells accents out (`Am'elie`) rather than dropping them | title folding is done in Python instead, to the same answers a glibc `iconv` gives |
| `rsync` is 2.6.9 (or openrsync), with no `--out-format` | the old `--log-format` spelling is used; `brew install rsync` gets a current one |
| ImageMagick 7 only, where `convert` is a deprecated wrapper | calls go to `magick` when the old name is gone |
| no `C.UTF-8` locale | (test suite) the same combination is assembled from `LC_COLLATE=C` and a UTF-8 `LC_CTYPE` |

Two things are **not** solved and will bite:

- **A case-insensitive filesystem.** APFS is case-insensitive by default, so a
  rename that only changes case, and two files that differ only in case, do not
  behave as they do on Linux. Keep the library on a case-**sensitive** volume.
- **Unicode normalisation.** macOS hands back decomposed (NFD) filenames, so a
  name with an accent is not byte-identical to the same name written on Linux.
  Nothing here re-normalises, so a library shared between the two can hold what
  looks like the same folder twice.

### Which ffmpeg a run uses

Every command that drives `ffmpeg` settles on one build before it starts and puts
it at the front of that run's `PATH`, so the whole run uses it — the command
itself, the parallel workers it spawns and the commands it calls. Whatever is on `PATH` wins: an
ffmpeg you put there is a deliberate choice. A run whose `PATH` has none — a cron
job, a systemd unit, a file-manager action — falls through to `$HOME/.local/bin`,
`/opt/homebrew/bin`, `/usr/local/bin` and `/opt/ffmpeg/bin`, where a
hand-installed or Homebrew build goes. Set
`ffmpegOverride` to an absolute path to pin one outright. `ffprobe` comes from the
same build, and a choice that is **not** what your own shell would have run is said
in the output; the ordinary case is silent.

[`convert-video`](#convert-video) goes one step further and asks each candidate
whether it can do what the chosen preset needs (see below).

> **Windows:** [`ytdlp`](#ytdlp) is the one that cares where it runs: it
> recognises a Windows-style host and translates the paths it hands yt-dlp, and
> `-s windows` / `-s linux` decides that outright.

## Audio

### `concat-audio`

Concatenates the audio in each input subfolder into one output file per
subfolder, building chapters (from a `.cue` file when present, otherwise from the
individual files) and embedding a cover thumbnail. Input names are only ever
touched when pretreatment is opted into.

### `convert-audio`

Transcodes spoken-word audio to Opus at a low bitrate, optionally forcing mono,
and carries over chapters and cover art. **Video files** are ingested too: their
audio stream is extracted and converted like any other input — and a video whose
soundtrack is *already* a small enough Opus is stream-copied out of its container
rather than encoded a second time into the same thing. **Long files** are
split at quiet points into chunks that encode in parallel and are transparently
re-joined, with the original's metadata re-attached. **Adaptive mode** decides
channels and bitrate per file: the output keeps the source's channel count and
gets the slightly higher bitrates allowing for sound-effects.

What gets re-encoded is decided per file: anything above the bitrate threshold,
anything in a format the output should not keep at all (`m4a`/`m4b`/`mka`), and
any video whose audio has to come out of its container. Everything else is small
enough already and is copied verbatim (`-c`) or left alone.

### `convert-and-concat`

Wrapper that ingests (`convert-audio`) and then concatenates
(`concat-audio`), keeping the intermediate Opus tree entirely in RAM, so only
the finished books are ever written to disk — which is why the output folder is
given rather than derived: it usually lives on a different disk than the source.

A book may arrive as an **archive instead of a folder**. Wherever the run expects a
folder of tracks — the input's subfolders, or theirs with `-s` — a `.zip`, `.rar`,
`.7z`, `.tar` or compressed tar lying there counts as one: it is unpacked into RAM
under its own name minus the suffix, and ingested from there like a folder of that
name. The archive itself is never touched, and only the extractors the input
actually needs are required.

- An archive beside a **folder of the same name** is left alone: that folder is
  taken to be it, already unpacked, and possibly corrected since.
- Of two archives claiming one name (a `.zip` and a `.rar` of one book), the first
  stands in for it and the other is passed over, so the two are never mixed into a
  single book.
- The redundant folder most archives carry (`Some Book.zip` holding `Some Book/…`)
  is dropped, so the tracks end up where they would have been unpacked by hand.

### `transcribe-audio`

Transcribes an input folder of audio and video with whisper, writing one
transcript per input into a mirrored output folder (the same sub-folder structure,
a new extension): `<input>/a/track.mp3` becomes `<output>/a/track.txt`. Audio
files are transcribed as they are; a video's speech is taken from its **first audio
track only** — the video stream and any further audio tracks are ignored, and a
video with no audio track is skipped with a warning.

The whisper model is settled for the host once, up front: the GPU when whisper can
really run on it (the biggest model the free VRAM holds), otherwise the CPU. A
transcript already present in the output is left in place, so a re-run only does
what is new. The format (`-f`) defaults to `txt`, and `-j` sets how many
transcriptions run at a time.

### `ingest-music`

Ingests a folder of freshly downloaded music into a clean, lossless library.
Everything is applied to the output only, so the download tree is never renamed.

- Multi-disc CUE+image rips sharing one folder are split into a subfolder each
  (de-duplicating with `fdupes` first).
- Every lossless source (FLAC/APE/ALAC/WAV/WavPack) is re-encoded to a normalised
  16-bit FLAC (capped at 48 kHz, embedded cover scaled to at most fullHD);
  everything else is copied across with `rsync`. Re-encoded FLACs keep their
  source's modification time.
- Large cover images become AVIF, stray video files are remuxed to MKV, and
  cue-sheet chapters are embedded into the flacs they describe.
- The assembled output is tagged and organised with `beets` (using the repo's
  the config that ships with the package, with the import log written to `logs/beets.log`), passed through
  `clean-folder-structure` for final name cleanup and empty-folder pruning, and
  120 kbps Opus copies are made with `convert-audio`.
- **Re-runs add only what is new.** A run ends by cleaning the library's names, so
  nothing in the library is called what this command called it — and a re-run must
  therefore not make all of it a second time. Running the same download folder again
  leaves the library exactly as it was.
- Cue sheets are read for their chapters first, and only the ones still without a FLAC
  of their own are then dropped.
- **Every phase counts what it is doing**, including the skips, so a re-run over a
  library that is already ingested says so per track instead of looking like an
  encoder that has stopped.

### `ytdlp`

Downloads the audio of a list of podcast feeds into a library laid out for a
phone, remembering what it has already fetched so a re-run only picks up what is
new. Sponsor segments are cut out, a thumbnail and metadata are embedded, and
Opus is preferred over m4a.

    ytdlp [options] <outputPath> <archiveFile> [<dateRange>]

The three arguments are the things that change between runs — where the library
goes, where the "already have it" record lives, and optionally an upload-date
filter (`20260607`, `20260607..20260707`, `..20260707`, or a relative date such
as `today-2weeks`). A rolling window is just a relative end: `..today-10days` is
"nothing newer than ten days". Everything else lives in the tables.

**One table, both systems.** A table (`data/podcasts/*.tsv`) holds one row per
podcast — its folder, its file-name template, how many entries back to walk (`0`
for the whole feed), any arguments it alone needs, and its URL — and the argument
sets every feed shares live once. The row does not say how yt-dlp is called,
because that is what differs between the two systems and is worked out at run
time:

- **which yt-dlp**: a `yt-dlp.exe` beside the checkout, one on `PATH`, or an
  importable `yt_dlp` module — which covers the apt, pip and pipx installs, and
  the single downloaded binary. On Linux a `yt-dlp-nightly` is taken ahead of
  the release one wherever both are installed, since an extractor a site broke
  is fixed in a nightly weeks before it is in a release
  (`pipx install --suffix=-nightly --pip-args=--pre yt-dlp` puts one there).
  `YTDLP` overrides the search. The winner is printed, because "it works on the
  other machine" usually starts there.
- **which nightly**: when the winner turns out to be a nightly, the run brings
  it up to date before fetching anything — a fix that landed this morning is no
  use to a build from last month. The install's own upgrade path is used (pipx
  for a pipx venv, `-U --update-to nightly` for a downloaded binary; a pip or
  distro install is left to its package manager), and a failed upgrade is a
  warning rather than the end of the run. `SKIP_YTDLP_UPGRADE=1` turns the
  check off, and a dry run (`-n`) never upgrades.
- **which paths**: under Git Bash or Cygwin the output and archive paths are
  translated to their drive-letter form, since a native `yt-dlp.exe` does not
  know where the emulated root is mounted.
- **which quoting**: `-n` prints the calls instead of running them, quoted for
  the shell of the host it is printed on. `-s windows` / `-s linux` prints the
  *other* machine's calls from the same table.

**Several tables, and what each one is.** `-t` may be given more than once, and
each table says what it is with a `#!profile` line:

| Profile | Fetches | At once |
| --- | --- | --- |
| `youtubeAudio` (default) | audio, sponsor reads cut out | one feed |
| `youtubeVideo` | video into Matroska, sponsor segments marked as chapters | one feed |
| `rssAudio` | audio, whatever the enclosure offers | ten feeds |
| `rssVideo` | video, likewise | ten feeds |
| `siteVideo` | a site that is neither: no SponsorBlock, no format ids, just the page's video | one page |

A `#!jobs <n>` line overrides a table's width and `-j` caps every table's
(downwards only). What may run alongside what is decided by the **provider**, not
by the table: two YouTube tables are one provider being asked for twice as much
at once, which is what gets a client throttled, so however many are given they
queue behind one another — while an RSS table is dozens of unrelated providers
who cannot see each other, so it runs alongside without waiting.

**Which tables there are.** One per thing that would otherwise have to be passed
differently — the output root, the download archive, or what the feeds are
fetched as. `podcasts.tsv` and `podcastsRss.tsv` are the phone's YouTube and RSS
feeds; `youtubeAudio.tsv` and `youtubeAudioSecondArchive.tsv` (the same feeds,
its own archive) the desktop audio library; `youtubeVideo.tsv` the video
library; `siteVideo.tsv` one site walked page by page; `youtubeAudioPaused.tsv`
the feeds that are switched off. Several may be given to one run with `-t`, as
long as they share that run's output root and archive.

The tables are one machine's library rather than code, so `data/` is not tracked
and there is a sample to start from instead —
[`medialib/config/podcasts.example.tsv`](medialib/config/podcasts.example.tsv), a working table
whose header documents the columns and the directives:

    cp medialib/config/podcasts.example.tsv data/podcasts/podcasts.tsv

A podcast is paused by putting a `0` in its `active` column, not by commenting
its row out — so it still shows up as a podcast, in a `grep` and in the count.
`-a` runs the paused ones anyway, `-m <text>` narrows a run to the feeds whose
folder or URL matches.

**Tidying up after a run (`-c`).** A yt-dlp run does not leave one file per
episode: it leaves the thumbnail it embedded, any description, metadata json or
subtitle files a row's `extraArgs` asked for, and — if it was interrupted — the
`.part` of a fragment. With `-c` the run clears that up afterwards: the
sidecars of the episodes **it** downloaded are removed (for video, the
description and the metadata json are first *attached into* the Matroska,
since they are the parts of a video that disappear with it), anything
that is not Matroska is remuxed into one, the leavings of interrupted downloads
are swept from the output root, and folders left empty are pruned. It works from
the run's own manifests rather than by scanning the library, which is what lets
it tell a leftover thumbnail from a folder's cover art.

**Building the phone's library from the run (`-i`).** What comes off the network
and what the phone plays are not the same file — the download is the best the feed
offers, the library copy is that re-encoded small enough to carry around — so with
`-i` they are not the same tree either. `<outputPath>` becomes the parent of both:

    phone/Staging/Speech/…       the raw downloads, kept
    phone/Staging/Music/…
    phone/Ingested/…             the library, built out of both

When the run is over, each staging folder's names are cleaned
(`clean-folder-structure`) and its episodes converted into that one library
(`convert-audio` — `-c -m` for podcast RSS, `-c -b 65` for music). There is one
staging folder per **conversion** and not per table or profile, because the
converter is handed a folder rather than a file — and they all end up in the
same library, so the split is invisible in the result. The raw downloads are
kept: they are the only copy of what was published, and the archive file says
they will not be fetched again, so changing one's mind about a bitrate is a
re-run of the last step rather than of the whole night. A video table ignores
`-i` and downloads to `<outputPath>` as usual, since an audio converter would
throw its picture away. Normally given together with `-c`, so the embedded
thumbnail is gone before the library is built out of the folder it was sitting
in.

**What a run prints.** One line per episode, and nothing else; `-v` puts yt-dlp's
own output back for when something needs diagnosing. The episodes are counted from
what yt-dlp reports after all post-processing, so the closing figures are of
finished files on disk rather than of feeds walked.

The tables, the date range and the tools are all checked before anything is
downloaded, and one feed failing (a renamed channel, a geo-block) does not stop
the run: the failures are collected and named at the end.

**Being refused.** YouTube increasingly answers with *"Sign in to confirm you're
not a bot"*. Every YouTube profile therefore asks as the android client
(`--extractor-args youtube:player-client=android`), which is handed the plain
player response rather than the challenge the default web client gets; a single
feed that needs another client can name one in its `extraArgs`, which come after
the profile's. When the refusal arrives anyway, yt-dlp has no special handling
for it — and because every feed is
fetched with `-i`, so that one dead episode does not end a feed, it would walk
into the refusal once more for every remaining episode, which is exactly what
deepens the block. So the run watches for it and stops **that provider** on the
spot: the feeds and tables queued behind it are not started, while tables of
other providers carry on untouched. It is said once where it happens and once
more at the end, and the run does not report success.

A run can also be stopped with Ctrl+C at any point: nothing that had not begun is
begun, and the run still reports what it managed before exiting — which is what
every command here does (see [Stopping a run](#stopping-a-run)).

## Video

### `convert-video`

Re-encodes a folder of videos into a clean, uniform library, mirroring each
source's name and sub-folder into the output. Video goes to a modern codec
(AV1/x265, **always 10-bit**), audio to Opus (surround downmixed to stereo), and
subtitle/attachment/chapter/metadata streams are copied across. The input tree is never modified, empty
output folders are pruned at the end, and reruns are no-ops (an output that
already spans its input is skipped).

**Hardware acceleration** is detected at runtime, so the same command behaves
correctly on a server and at the desktop: an NVIDIA GPU with NVENC plus a
hardware profile encodes on NVENC, an Intel iGPU is used as decoder even for
software profiles, and missing hardware falls back gracefully to software.
**Parallelism is per file, not across files** — one file already saturates the
encoder, so files are processed one at a time and cut into chunks that are
re-concatenated transparently.

**Resolution is a ceiling, not a target.** `-r` caps the output at a resolution
tier — named either by its line count (`720p` … `4320p`) or by a marketing name
(`fullHD`, `2K`, `4K`, `UltraHD`, `8K`, …), in any case — and everything above it
is scaled down to fit with its aspect ratio kept, so a 2.39:1 scope film capped at
1080p comes out 1920x800 rather than letterboxed. Anything already at or below the
tier is encoded at its own size: **nothing is ever scaled up.** The tiers are the
same ones `content-census-bi` reports a library by.

**A newer ffmpeg is preferred if the preset needs one.** The AV1 presets ask for
psychovisual SVT-AV1 parameters and the NVENC ones for `uhq` tuning, which a
distribution's ffmpeg is often a year or two too old to do — and a too-old SVT-AV1
**drops a parameter it does not know instead of failing**, so a build that applies
half the tuning looks exactly like one that applies all of it. So each candidate
build (see [above](#which-ffmpeg-a-run-uses)) is asked to encode a single frame with
the preset's own arguments, and the first that takes all of them is used. The run
says which build it settled on and warns when that build cannot do everything the
preset asks.

**How hard each file is encoded is decided per file, not per preset.** A preset
states one quality level, and that level is then moved by the tier the file is
*encoded* at — a 2160p file two levels softer, an SD file two levels harder — since
the same number does not buy the same visible quality across the ladder. A source
capped by `-r` is judged by the size it comes out at, not the one it arrived at.
`-q` names a level yourself and turns the bias off.

**Film grain follows the source.** Every preset that synthesises grain measures each
file and synthesises what it measured, so a clean digital master and a 16mm blow-up
are not handed the same number; `av1Animation` synthesises none, which is the one case
where grain is actively wrong. `-g` names a level yourself and nothing caps it, and
`-g off` turns it off. This is **lossy and irreversible**: the grain is denoised out of
the stored picture and a player re-generates a similar-looking one, so what comes back
is an imitation of it.

**`-t` converts only what has room to save.** A re-encode is worth its hours when the
source has bits to spare, and worth nothing at all when it does not — a starved file
only comes back starved a generation further on. So `-t` measures each source's *video*
bitrate first (the audio is left out of it) and asks two questions before encoding
anything. Is the source at least **adequate** for what it is — its codec, frame size,
aspect ratio, frame rate and measured grain? If not, it is skipped: nothing here can
improve it. And would this run's own output still be adequate on half the bitrate? If
not, it is skipped too, with the figures it was judged on. `-t 30` is a looser run
(convert anything that can save 30%) and `-t 0` keeps only the starved check.

**Interlaced and anamorphic sources are reported, not corrected** — such a file
gets a warning and is encoded exactly as it arrived. Deinterlace or un-squeeze
upstream if you want that done.

**HDR and Dolby Vision.** HDR10 metadata is preserved when present, and DV is
carried through wherever the encoder can code an RPU — the software encoders can,
NVENC cannot, and keeping DV needs ffmpeg 7.1 or newer. Which files really keep
it is decided per file, and the finished file is checked to still signal DV. A
**dual-layer profile 7** source — whose RPU no encoder can re-encode — is
normalised to single-layer profile 8.1 first, the same no-re-encode conversion
`ingest-movies` does, so it keeps its Dolby Vision without being ingested
beforehand; that needs `dovi_tool` and `mkvmerge`. Where DV cannot be kept (a
hardware profile, or a normalisation that could not run) the reason is reported
and the HDR10 layer is preserved, so such a file comes out as plain HDR10 rather
than losing its high dynamic range.

**The video encode is never lost to a failure of the cheap steps.** It costs
orders of magnitude more than everything else, so if the audio or the mux fails
the finished video is still written out as `<name> (video only).mkv` with a
warning, and a later rerun that completes properly supersedes and deletes it.
Only an incomplete video encode itself is discarded. Nor is it lost to the place
it was going: an output sub-folder deleted while the file was encoding is put
back to write into, and a name taken in the meantime by something else gets the
encode written beside it as `<name> (2).mkv` rather than over the top.

**A run can be paused and resumed from the keyboard.** Press `p` and every video
encoder of the moment stops where it is — however the work is being spread, over the
CPU's chunks or the GPU's engines — so the cores or the NVENC engines are free for
something else; press `r` and they all carry on from exactly where they stopped. This
frees **computation, not memory**: the encoders are stopped, not unloaded, so their RAM
and VRAM stay allocated, and the pause lives and dies with the run itself (there
is no pausing across a reboot, or from another terminal). Audio, one short
single-threaded process per track, deliberately keeps going. `Ctrl+C` still ends the
run, paused or not, and the time spent paused is reported separately and kept out of
the run's throughput figures. `ffmpeg` itself has no pause — the keys are the command's,
and what they move is the operating system's stop/continue signal.

### `ingest-movies`

Sorts loose movie files into per-movie subfolders (Plex layout), cleans
folder/movie/subtitle names, renames/downloads subtitles for six languages,
refreshes mkv tags, transcodes audio to Opus, and transcribes commentary tracks.

A track counts as a commentary when its mkv commentary flag says so *or* when its
name does — in any of the supported languages, so a German disc's
"Audiokommentar" is picked up as readily as an "Audio Commentary". Commentary is
transcribed **in the language it is spoken in**: English gets an English
subtitle, another supported language gets the native one *and* an English
translation, anything else only the translation.

As a final step it remuxes an improved copy of each film in **one** `mkvmerge`
call, so the film is remuxed once and only one output exists at a time:

- Lossless tracks that were converted to Opus are swapped in place, keeping
  index, language, name and flags.
- Excessive audio and image-subtitle tracks are dropped. Commentary **audio** is
  exempt from the excessive-language rule, so a foreign-language commentary is
  never dropped from the film its subtitles were just made for.
- Commentary transcripts are appended, each tagged with its own language.
- **Dual-layer Dolby Vision profile 7 is normalised to single-layer profile
  8.1**, so DV survives on hardware that cannot read the enhancement layer. This
  is metadata only — the video is **never re-encoded** — and profile 5, profile
  8.x, HDR10, HDR10+ and SDR are left untouched.
- **A Dolby Vision claim the video does not back up is dropped**, so the file
  stops lying about itself: what the container reports is only a claim, so every
  DV file's stream is probed for an RPU that is really there, and a hollow claim
  is stripped, leaving the file reporting what its bitstream really is. A file
  whose RPU is really there is only checked, never rewritten, and a copy that
  came out less HDR than the source is thrown away and the original left alone.

The original is preserved as `<name> (old).mkv`, so nothing is lost and a rerun
is a no-op — which is also what makes the Dolby Vision work a one-time job per
film: a folder that already carries a `(old)` backup is skipped whole.

## Books, comics and images

### `ingest-books`

Ingests a folder of e-books into a clean, uniform library, mirroring each
source's name and sub-folder into the output. Books are processed in parallel
across all cores, and only the finished file is written to disk:

- PDFs are stripped of their (usually oversized) images and copied across.
- `mobi`/`chm`/`azw3`/`lit`/`txt` sources are converted to epub.
- Epub sources (and the just-converted epubs) are unpacked, cleaned (embedded
  fonts dropped, images downscaled to at most fullHD, junk/teaser images removed
  via an extensible name-substring list) and repacked, then re-converted once
  more for consistent readability.

The input tree is never modified, and emitted books never clobber an existing
output (a collision keeps both via a ` (N)` suffix).

### `read-library`

Reads a whole library of e-books aloud into **two** audiobook libraries side by
side — one per format — each mirroring the book's name and sub-folder:
`<in>/Fiction/Author/Title.epub` becomes `<out>/opus/Fiction/Author/Title.opus`
**and** `<out>/flac/Fiction/Author/Title.flac`. The narration is done by a local
[ebook2audiobook](https://github.com/DrewThomasson/ebook2audiobook) checkout,
driven headless; this command is the library-level work around it — which books, in
what order, how many at a time, and what the run cost.

- **Two files per book, both complete.** A 36 kbps mono Opus to listen to and a
  lossless FLAC to keep, each with the book's chapter marks and its cover art in
  it. `-b` changes the Opus bitrate, `-b 0` writes none, `-o` keeps only the Opus.
- **One folder per format, each a whole library.** `<out>/opus` goes on a phone and
  `<out>/flac` on the archive disk, synced or backed up independently of each
  other. The folder is the extension itself, so a book whose lossless copy could
  only be the engine's own `.m4b` lands in `<out>/m4b` rather than among the FLACs.
- **Each book's language is established, not assumed.** A TTS engine is *told*
  what language its input is in and detects nothing, so a book read without that
  is read out by an English speaker. The language comes from the book's own
  metadata, and from its text when the metadata says nothing. `-l` sets one for
  the whole run instead; a language the engine cannot speak is refused up front
  rather than per book, hours into a queue.
- **Voice cloning** is optional (`-v`): any audio or video file will do, in any
  format and any length. Point `-v` at a **directory** instead and it is one voice
  *per language* (`deu.wav`, `german.m4a`, `de.mp3`, plus an optional
  `default.wav`): a cloned voice carries the accent of its sample, so a German
  book read by a clone of an English speaker is read with an English mouth for
  nine hours.
- **The longest book is read first.** Every book's word count — very nearly its
  running time — is measured before the first one is narrated, so a nine-hour book
  cannot land at the back of the queue and hold the device on its own after
  everything else has drained. PDFs are measured with `pdftotext`, the rest with
  Calibre; without either, the queue falls back to file size, which reads an
  illustrated e-book or a scanned PDF as though it were long.
- **As many books at a time as the device has room for** (free VRAM ÷ 5 GB on a
  GPU, one on a CPU; `-j` overrides).
- **Reruns resume.** A book is either fully in the library or not in it at all, so
  an interrupted run costs at most the book it was reading. A book that produces
  nothing is reported and the run carries on.

### `convert-comics`

Recompresses a tree of comic books — `.cbr`/`.cbz`/`.cb7` archives, and PDFs that
hold one large image per page — into a tree of `.cbz` archives of AVIF pages.

- Each book is extracted and flattened into a single folder of pages, the pages
  are converted to AVIF, and their names are cleaned and numbered.
- A comic that arrived as a **PDF** is treated as the same book in a different
  container: its pages are rendered out at the resolution the PDF's own page
  images report, and everything after that is the archive path unchanged.
- Unlike an archive, a PDF does not announce that it *is* a comic, so every PDF is
  inspected first and only converted when nearly all of its pages hold exactly
  one image covering (nearly) the whole page. A magazine, a manual or a text
  e-book therefore does not get rasterised page by page into a large, unreadable
  `.cbz`: it is reported with the numbers it was judged on and left alone.
- Each finished folder of pages is zipped back into one **stored** (level 0 —
  AVIF does not compress further) `.cbz` in its mirrored parent folder.
- Pages and AVIFs are intermediate and never leave RAM: the `.cbz` files are the
  only thing written to disk, so the output tree has the same depth and roughly
  the same file count as the input rather than one extra folder level per book.
- A `.cbz` left by an earlier run is skipped, so re-running over a grown library
  neither duplicates nor rewrites finished books.

### `convert-images`

Batch-converts images to AVIF (or back to JPEG), with optional whitespace
cropping and parallel encoding.

## Names and folder structure

### `clean-folder-structure`

Applies the shared name cleaners in place across a nested folder tree. As a final
tidy-up it removes any sub-folders left empty (or already empty), keeping the
input root itself. It can also number the plurality filetype in each folder, sort
`YYYYMMDD` files into yearly subfolders, and simulate a whole run in a sandbox —
writing `before.tree` and `after.tree` into the input folder for risk-free review
without touching anything.

A phone mounted over MTP is handled automatically, and needs nothing set up on
the phone. Most mounts rename in place at metadata speed — an MTP rename sets an
object's name property, so no file content moves — and the folder is then cleaned
like any other. A mount that refuses renames is cleaned locally instead, with
only the resulting renames replayed on the device over `adb`, which does require
USB debugging. Which of the two applies is settled by trying a rename, not
assumed. The path may be given in either spelling the file manager offers — the
`mtp://…/SD%20card/…` URI it copies, or the `/run/user/…/gvfs/mtp:host=…` path
that URI stands for.

Renaming itself is done in three phases, reused by the comic and audio pipelines
too:

1. **Individual** — clean each name on its own, normalise separators and
   punctuation, remove configured fragments, and split off a leading date/number
   prefix.
2. **Collective** — remove the longest leading/trailing affix a group of sibling
   names has in common, without cutting through a word or number and without
   unbalancing brackets.
3. **Hidden-prefix recovery** — restore prefixes the collective pass would
   otherwise have hidden.

The fragments to remove are read from `data/fragments.txt` (one per line), or from
a file named with `-f`. Without either, names are cleaned without any fragment
removal.
[`find-fragment-candidates`](#find-fragment-candidates) surfaces the recurring
leftovers in a library worth adding to that list.

Fragment removal and affix stripping are **word/number aware**: nothing is
removed when doing so would slice through the middle of a word or a number, so
`must be better` is never mangled by a `tt` fragment.

### `find-fragment-candidates`

Reports the recurring fragments a library's names still carry — the release tags,
site names and encoder marks the individual cleaning pass would strip if it knew
about them — so they can be reviewed and added to `data/fragments.txt`. It reads
nothing but names and writes nothing but its report: no file is touched.

Point it at a folder, and a tree of that folder is generated and parsed; point it
at a tree file that already exists, and that is parsed directly. The report lands
beside whichever it was given, as `fragmentCandidates.txt`, or wherever `-o`
says.

`-m` is the prevalence floor: how many distinct names have to carry a candidate
before it is worth reporting. The default of 2 hides one-off title words, which
is most of what a first run would otherwise show; `-m 1` lists every candidate.

### `cue-to-chapters`

Converts a `.cue` sheet into an OGM chapter file (`CHAPTERNN=` /
`CHAPTERNNNAME=`), which is what the audio pipelines feed to `mkvmerge`.

## Library census

### `content-census`

Reads a library and writes down what is in it: one row per file, one report per
content type (audio, video, books, comics), written into the folder that was
censused and named after it. Every file whose suffix is in one of the central
extension lists is included; nothing in the tree is renamed, moved or converted.

**Several libraries can be censused in one call**, each as its own library with
its own reports — which is what makes the `library` axis of
[`content-census-bi`](#content-census-bi) usable without doing the runs by hand.

`-b` builds the cubes in the same command, and is only a shorthand, deliberately
so: the two halves have very different costs. The census reads every file and
converts every book to count its words — the hour-long half — while the cubes are
seconds of arithmetic over the finished CSVs. When the reports already exist, run
`content-census-bi` on its own and don't pay for the walk again.

`-o`'s folder is **made if it is not there yet** — pointing a run at a fresh
`~/reports/tonight` needs no `mkdir` first — and given back again if the run then
refuses, so "nothing was changed" stays true of directories too.

Two folders **named the same** are refused up front, naming both, rather than
censused: their reports would collide, and since a library is known by its report
name from there on, the failure would otherwise be silent.

**`-d` takes the libraries from below the paths given.** A disk is usually not
one library but a shelf of them, and naming forty by hand is not an answer:

```
content-census -b -d 1 -o ~/reports /mnt/discOne /mnt/discTwo
```

`-d 1` makes every subfolder of a given path a library of its own, `-d 2` every
grandchild, and so on; `-d 0` (the default) censuses the paths themselves.
Exactly that level is a library and everything below it belongs to it — a file
lying *beside* those folders, or above them next to the path given, belongs to no
library and is not censused. Each library gets its own reports, named after the
way down to it (`videoDiscOneFilms.csv`), so
[`content-census-bi`](#content-census-bi)'s `library` axis gets a level to drill
down into without a second census.

- **Dynamic range** is read through the same judgements
  [`ingest-movies`](#ingest-movies) acts on, so the two cannot disagree about a
  file. What the container *claims* is what is recorded: verifying that a Dolby
  Vision RPU is really in the bitstream costs a pass over the whole stream, which
  is the ingest's job, not a census's.
- **Every video is judged starved, adequate or generous** for what it is, in a
  column of its own. Whether a bitrate is enough depends on the codec, the frame
  size and the frame rate together, so no single column can answer it — and the
  answer is what "which of this is worth re-encoding" actually asks. It is taken
  with the very model [`convert-video`](#convert-video)'s `-t` decides one file
  with, so the census and the conversion cannot disagree about a film, and it becomes an axis of the cube like any other. It costs the
  census nothing: every input but one is already in the header being read, and that
  one — how grainy the picture is, which has to be measured off the pixels — is
  switched off here, because a decode per file is not a census. Grain only ever
  *raises* what a stream needs, so nothing is called starved that a run which did
  measure it would call adequate. A file whose bitrate or frame size nobody stated
  reads `unknown`.
- **Books are counted on their text**: each one is converted with Calibre and
  counted from that, so epub, mobi, chm, azw3, lit and PDF all answer the same
  way instead of each needing its own extractor.
- **A file with no chapter marks counts as one chapter**, not as none: it is one
  unbroken chapter, itself. So the `chapters` column adds up to how many parts a
  library holds rather than to how many somebody happened to mark up, and a mean
  chapter length is not a division by zero.
- **A `.pdf` is decided, not assumed**: it is in both the comic and the book
  extension lists, so which report it lands in comes from the same probe
  [`convert-comics`](#convert-comics) gates on — a scan goes to comics, a text
  book to books.
- **A lying suffix is skipped, not guessed at**: an `.mka` that holds a video
  track, an `.mp4` with no video in it, a `.cbz` that is not an archive or holds
  no page. Each one is named at the end of the run, with the reason.
- **Missing optional tools cost a column, not the run**: without `mediainfo` the
  dynamic range comes from ffprobe instead (HDR10+ then reads as HDR10), without
  poppler-utils no PDF can be examined, without Calibre the word and character
  counts stay empty. Each is said once at startup rather than discovered per file.

The census is **serial within a disk and parallel across them**. Reading one
library is one metadata read per file across a whole tree, which on the spinning
disks such a library lives on is seek-bound: eight of those at once over one disk
finish later than in order. Several `<inputPath>`s, though, are several disks, so
the run forks one worker per path given and no more — and the libraries `-d`
finds *under* one path are censused one after the other inside its worker, since
they share its head. Books are the exception to the serial half: their cost is a
text conversion on the CPU, not a read on the disk, so the books of a library run
one per core while the rest of it still goes through one at a time. `Ctrl+C`
stops the walk but still writes the reports for the files already read, in every
library that was being read.

### `content-census-bi`

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
**nothing installed**: no server, no DuckDB, no Python. The data is embedded and
never leaves the file; only the Perspective engine itself is loaded from a CDN,
so the page needs the network the *first* time it is opened on a machine and
nothing after that, and the library listing is never sent anywhere.

**The page reads in human units**: sizes in gigabytes (1 GB = 1,000,000,000
bytes) and durations as `hours:minutes`. Only the page — the reports, the fact
views and the cubes keep the exact bytes and seconds a probe reported. Both are a
multiplication by a constant, so every roll-up still adds up.

The database is rebuilt from nothing on every run and says so — a cube is derived
data with no history in it. Every refusal happens before that, so a run that is
refused leaves the previous database intact.

## File safety

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

### The input folder itself

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

### Stopping a run

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

A second `Ctrl+C` during the wind-down is not caught, so an impatient user can
always get out — even when what is being waited for is a `rm -rf` over a mount that
has gone away.

Two runs deliberately do more than stop where they are: `content-census` still
writes the reports for the files it already read (but does **not** build cubes from
them, since a cube cannot say how complete the census behind it is), and `ytdlp`
finishes the download in flight before stopping.

### Pausing a run

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

### The output folder must not sit inside the input

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

### What leaves the machine

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
  table.
- **`content-census-bi`** writes an `.html` page that loads the Perspective
  engine from jsDelivr when you open it, pinned to an exact release with the
  stylesheet checked against a hash. Your census data is not uploaded anywhere —
  it is embedded in the file itself, which is why the page is worth as much
  offline as the engine's cache allows.

## License / credits

MIT — see [LICENSE](LICENSE). Use it, change it, ship it; there is no warranty.

Authored by David Ernst, qwen and claude opus.
