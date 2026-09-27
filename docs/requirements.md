# Requirements in detail

The list of what to install is in the [README](../README.md#requirements); this
page is what the commands do about it.

## Missing tools are refused up front

Each command checks the external tools it is about to drive **before it touches
anything**, and refuses the run naming *every* missing one at once, what it is
needed for and how to install it. The list is **what this run needs**, not
everything the command can ever use: `ingest-books -t` asks for Calibre alone,
and `convert-comics` only asks for `unrar` when the input actually holds a
`.cbr`.

## …unless the run can do its job anyway

Only a tool whose absence would spoil the output, or silently drop part of it, is
worth refusing over. The rest say what they cannot do and the run goes on:

| Missing | What happens instead |
| --- | --- |
| `dovi_tool` (or `mkvmerge`) | the Dolby Vision file is left exactly as it came in, and a dual-layer profile 7 source is re-encoded as plain HDR10 |
| `hdr10plus_tool` (or `mkvmerge`) | an HDR10+ source is re-encoded as plain HDR10, with a warning naming the missing tool |
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

## Running on a Mac

Everything here is written to work on macOS and nothing is written *only* for
it. This is aspirational — no Mac has run it — so report anything that does
not hold.

Three things are **not** solved and will bite:

- No NVENC. While hardware **decode** goes through VideoToolbox;
  **encoding** is the software profiles (`av1Svt`, `x265`)
  the `*Nvenc` profiles need an NVIDIA card and refuse up front.
- **A case-insensitive filesystem.** APFS is case-insensitive by default, so a
  rename that only changes case, and two files that differ only in case, do not
  behave as they do on Linux. Keep the library on a case-**sensitive** volume.
- **Unicode normalisation.** macOS hands back decomposed (NFD) filenames, so a
  name with an accent is not byte-identical to the same name written on Linux.
  Nothing here re-normalises, so a library shared between the two can hold what
  looks like the same folder twice.

## Which ffmpeg a run uses

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

[`convert-video`](commands/convert-video.md) goes one step further and asks each candidate
whether it can do what the chosen preset needs, and
[`ytdlp`](commands/ytdlp.md) asks each one whether it runs at all, then names the winner to
yt-dlp, which would otherwise search `PATH` for a build of its own.

> **Windows:** [`ytdlp`](commands/ytdlp.md) is the one that cares where it runs: it
> recognises a Windows-style host and translates the paths it hands yt-dlp, and
> `-s windows` / `-s linux` decides that outright.
