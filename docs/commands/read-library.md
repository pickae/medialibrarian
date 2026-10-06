# `read-library`

Reads a whole library of e-books aloud into **two** audiobook libraries side by
side — one per format — using a local
[ebook2audiobook](https://github.com/DrewThomasson/ebook2audiobook) checkout,
driven headless.

    read-library [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | a folder scanned recursively for e-books |
| **Writes** | `<outputDir>/opus/…` and `<outputDir>/flac/…`, each mirroring the book's name and sub-folder |
| **Input** | never modified |
| **Reruns** | resume: a book is either fully in the library or not in it at all, so an interrupted run costs at most the book it was reading |
| **Network** | none of its own — whatever the checkout fetches for itself is the checkout's |

## Default behavior

With no options, a run:

- reads each book with the `xtts` engine in its own voice, on the GPU when there is one (`-r`, `-d`)
- **picks each book's language** from its metadata, then its text, falling back to English (`-l`)
- writes a 36 kbps mono Opus **and** a lossless FLAC of every book (`-b`, `-o`; [below](#the-two-libraries))
- reads as many books at once as the free VRAM holds, longest first (`-j`)
- skips a book that is already in the library

`<in>/Fiction/Author/Title.epub` becomes `<out>/opus/Fiction/Author/Title.opus`
**and** `<out>/flac/Fiction/Author/Title.flac`. The narration is the checkout's;
this command is the library-level work around it — which books, in what order,
how many at a time, and what the run cost.

## Setup

It needs a local ebook2audiobook checkout and a Python 3.10–3.12 to build its
environment from; everything inside that environment is installed by the
checkout itself.

| Setting | Default | What it is |
| --- | --- | --- |
| `-c <dir>` or `narrationHome` | `~/ebook2audiobook` | the checkout to drive |
| `-d <device>` | `cuda` with an NVIDIA GPU, `cpu` otherwise | where the model runs |
| `-e <engine>` | `xtts` | the TTS engine; voice cloning needs one that supports it |

## The two libraries

**Two files per book, both complete.** A 36 kbps mono Opus to listen to and a
lossless FLAC to keep, each with the book's chapter marks and its cover art in
it.

| Option | Opus copy | Lossless copy |
| --- | --- | --- |
| default | 36 kbps | FLAC |
| `-b <kbps>` | at that bitrate | FLAC |
| `-b 0` | none | FLAC |
| `-o` | 36 kbps | none |

**One folder per format, each a whole library.** `<out>/opus` goes on a phone and
`<out>/flac` on the archive disk, synced or backed up independently of each
other. The folder is the extension itself, so a book whose lossless copy could
only be the engine's own `.m4b` lands in `<out>/m4b` rather than among the FLACs.

## Language and voice

**Each book's language is established, not assumed.** A TTS engine is *told*
what language its input is in and detects nothing, so a book read without that
is read out by an English speaker. The language comes from the book's own
metadata, and from its text when the metadata says nothing. `-l` sets one for
the whole run instead; a language the engine cannot speak is refused up front
rather than per book, hours into a queue.

**Voice cloning** is optional (`-r`):

| `-r` given | Voice |
| --- | --- |
| nothing | the engine's own |
| a **file** | cloned from it for every book: any audio or video file will do, in any format and any length |
| a **directory** | one voice *per language* (`deu.wav`, `german.m4a`, `de.mp3`, plus an optional `default.wav`): a cloned voice carries the accent of its sample, so a German book read by a clone of an English speaker is read with an English mouth for nine hours |

## Scheduling

- **The longest book is read first.** Every book's word count — very nearly its
  running time — is measured before the first one is narrated, so a nine-hour book
  cannot land at the back of the queue and hold the device on its own after
  everything else has drained. PDFs are measured with `pdftotext`, the rest with
  Calibre; without either, the queue falls back to file size, which reads an
  illustrated e-book or a scanned PDF as though it were long.
- **As many books at a time as the device has room for** (free VRAM ÷ 5 GB on a
  GPU, one on a CPU; `-j` overrides).
- **A book that produces nothing** is reported and the run carries on.

---

**Options:** `read-library -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses) ·
[what a run prints](../output.md)
