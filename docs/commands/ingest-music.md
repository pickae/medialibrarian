# `ingest-music`

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
