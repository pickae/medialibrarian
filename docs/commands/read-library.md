# `read-library`

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
