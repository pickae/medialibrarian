# Media management commands

Command-line tools for ingesting, transcoding and tidying up personal media
libraries — audiobooks, music, movies, comics and image galleries. Every one that
renames files follows the same "never clobber, never lose a file" rules,
described under [File safety](docs/file-safety.md). What they print, and the `-v` and
`-q` that set how much, is described under
[What a run prints](docs/output.md).

Each is an installed command. From a checkout, `pip install .` puts all nineteen
on your PATH; `pip install -e .` does the same and keeps them running the
checkout, which is what you want while editing it. Under a plain `pip install .`
the per-machine files — the `data/` tables, the `logs/` logs — live under
`~/.local/share/medialib` (or `$XDG_DATA_HOME/medialib` if it is set); the
environment variable `CLI_SCRIPT_DIR` points them somewhere else.

Heavy intermediate work is kept in RAM (`/dev/shm` / tmpfs) wherever possible, so
only final outputs are written back to disk. Each run gets a scratch directory of
its own in there, so several can run at once without sharing one.

> **Platform:** these target **Linux**. **macOS** support is **aspirational**:
> everything is written to work there, but no macOS machine has run it yet, so
> treat it as untested rather than as broken (see
> [Running on a Mac](docs/requirements.md#running-on-a-mac)). On a Windows machine **WSL2** is the
> way to run them. Native Windows is **experimental for now**: the tool-free
> cases import and run there, but the commands that drive the media tools and
> POSIX coreutils expect a POSIX host. Neither Windows nor macOS has a
> RAM-backed filesystem, so the scratch above lands in `%TEMP%` / `$TMPDIR` on a
> normal disk; a RAM disk mounted by hand works if `ramScratchBase` points at
> it.

## At a glance

Each command has a page of its own under [`docs/commands/`](docs/commands/), laid
out the same way: what it does and its usage line, an *at a glance* table (what it
takes and writes, whether it changes the input, what a rerun does, whether it
reaches the network), then the details, and at the foot the shared rules that
apply to it.

### Audio

| Command | Does |
| --- | --- |
| [`concat-audio`](docs/commands/concat-audio.md) | One audiobook file per input subfolder, with chapters |
| [`convert-audio`](docs/commands/convert-audio.md) | Spoken-word audio → low-bitrate Opus or xHE-AAC |
| [`convert-and-concat`](docs/commands/convert-and-concat.md) | The two above chained, intermediate tree in RAM |
| [`transcribe-audio`](docs/commands/transcribe-audio.md) | Audio and video → whisper transcripts in a mirrored folder |
| [`ingest-music`](docs/commands/ingest-music.md) | Downloads → clean lossless FLAC library |
| [`ytdlp`](docs/commands/ytdlp.md) | Download tables of podcast feeds as audio or video, Windows or Linux |

### Video

| Command | Does |
| --- | --- |
| [`convert-video`](docs/commands/convert-video.md) | Re-encode a video library (AV1/x265 + Opus) |
| [`ingest-movies`](docs/commands/ingest-movies.md) | Sort, clean and improve a movie library in place |

### Books, comics and images

| Command | Does |
| --- | --- |
| [`ingest-books`](docs/commands/ingest-books.md) | E-books → clean, uniform epub/pdf library |
| [`read-library`](docs/commands/read-library.md) | E-books → audiobooks (Opus + lossless), read aloud by a TTS engine |
| [`convert-comics`](docs/commands/convert-comics.md) | `.cbr`/`.cbz`/`.cb7` and comic PDFs → `.cbz` of AVIF pages |
| [`convert-images`](docs/commands/convert-images.md) | Batch image → AVIF / WebP / JPEG XL (or back to JPEG) |

### Names and folder structure

| Command | Does |
| --- | --- |
| [`clean-folder-structure`](docs/commands/clean-folder-structure.md) | Apply the shared name cleaners across a tree |
| [`find-fragment-candidates`](docs/commands/find-fragment-candidates.md) | Report the recurring name fragments a library still carries |
| [`find-gaps`](docs/commands/find-gaps.md) | A tree of what is missing from the numbered, episode and dated runs |
| [`cue-to-chapters`](docs/commands/cue-to-chapters.md) | A `.cue` sheet → an OGM chapter file |

### Library census

| Command | Does |
| --- | --- |
| [`content-census`](docs/commands/content-census.md) | Census one or more libraries into one CSV per content type |
| [`content-census-bi`](docs/commands/content-census-bi.md) | Roll those reports up into DuckDB hypercubes and a pivot-table page |

**Every command prints its arguments, options and defaults with `-h`** (or
`--help`), which needs none of the media tools installed — so these pages are about
what the commands do and the rules they all follow, and name a flag only where it
changes what a command does.
`cue-to-chapters` is the one exception: it takes two file names and no options,
so any argument list but those two prints its usage and fails.

Every option has both forms: `-j 8` and `--jobs 8` (or `--jobs=8`) are the same
option. Abbreviations are not accepted — `--job` is not `--jobs` — so a new
option can never change what a command you already type means.

## Requirements

- Python 3.11.4+, and one package with it: `mutagen`, which the install brings in
- A UTF-8 locale (accented/multibyte filenames are handled character-wise)
- Per-tool external dependencies (see each command's `-h` output). Across the set:
  `ffmpeg`, `mkvtoolnix`, `dovi_tool`, `hdr10plus_tool`, `mediainfo`, `ImageMagick`, `rsync`,
  `yt-dlp`, `fdupes`, `unrar`/`unzip`/`7z`/`tar`/`zstd`, `zip`, poppler-utils (`pdftoppm`,
  `pdfinfo`, `pdfimages`, `pdftotext`),
  `whisper-ctranslate2`, `ffsubsync`, `tree`, `beets`, Calibre's `ebook-convert`,
  Ghostscript (`gs`), `duckdb`, [subcleaner](https://github.com/KBlixt/subcleaner)
  and `wc` — that last one being the only piece of coreutils anything here still
  shells out to.
- `convert-audio -o xheaac` additionally needs
  [exhale](https://gitlab.com/ecodis/exhale), which ffmpeg cannot stand in for:
  it decodes the codec but does not encode it. No distribution packages exhale,
  so it has to be built (see [`convert-audio`](docs/commands/convert-audio.md)).
- `read-library` additionally needs a local
  [ebook2audiobook](https://github.com/DrewThomasson/ebook2audiobook) checkout and
  a Python 3.10–3.12 to build its environment from; everything inside that
  environment is installed by the checkout itself (see
  [`read-library`](docs/commands/read-library.md)).
- `convert-video -u` additionally needs an NVIDIA GPU and a VapourSynth +
  TensorRT environment of its own, one piece of which has to be built (see
  [`convert-video`](docs/commands/convert-video.md)).

What a run does when one of these is missing, what does not work on a Mac yet,
and which ffmpeg build a run picks are in
[Requirements in detail](docs/requirements.md).

## File safety

Renaming never overwrites an existing file and never writes outside the input
and output folders you gave, and the renames skipped for safety are listed at
the end of the run. A run can be stopped at any point and still reports what it
did. The full rules, including which commands reach the network, are in
[File safety](docs/file-safety.md).

## License / credits

MIT — see [LICENSE](LICENSE). Use it, change it, ship it; there is no warranty.

Authored by David Ernst, qwen and claude opus.
