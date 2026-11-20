# `ingest-music`

Ingests a folder of freshly downloaded music into a clean, lossless FLAC
library, plus Opus copies to carry around.

    ingest-music [options] <downloadDir> <ingestDir> [opusCopyDir]

| At a glance | |
| --- | --- |
| **Takes** | a folder of downloaded music |
| **Writes** | the FLAC library into `<ingestDir>`, and 120 kbps Opus copies into `<opusCopyDir>` (default: the sibling folder `<ingestDir>opus`) |
| **Input** | de-duplicated and multi-disc rips split into subfolders - on a copy of the tree with `-k downloads`, which keeps the folder as it was; everything else is applied to the output only |
| **Reruns** | add only what is new: running the same download folder again leaves the library exactly as it was |
| **Network** | yes — beets asks AcoustID, MusicBrainz, Last.fm, a lyrics site and a cover-art host, and any unmatched album may be looked up on MusicBrainz through a web-searching language model (`musicSearchLlm`), unless `-o` skips the tagging phase ([details](../file-safety.md#what-leaves-the-machine)) |

## Default behavior

With no options, a run:

- **de-duplicates the download folder and splits multi-disc rips there**, deleting identical files (on a copy of the tree with `-k downloads`, so the folder itself comes out of the run as it went in)
- re-encodes lossless tracks to 16-bit FLAC at most 48 kHz (the source's own resolution with `-k`), and copies the rest
- **tags and fetches covers, lyrics and genres with beets** (online; the phase is skipped with `-o`, and the config can be replaced with `-c`)
- cleans the names with [`clean-folder-structure`](clean-folder-structure.md)
- makes 120 kbps Opus copies into `<ingestDir>opus` (`[opusCopyDir]`)
- adds only what is new on a rerun

Each phase is in the [table below](#what-it-does).

## What it does

| Phase | What happens |
| --- | --- |
| Split | multi-disc CUE+image rips sharing one folder are split into a subfolder each (de-duplicating with `fdupes` first). With `-k downloads` the de-duplication and the split happen on a copy of the tree, and a copy that cannot be made leaves the folders the split would take apart refused - not ingested flat, and not changed |
| Encode | every lossless source (FLAC/APE/ALAC/WAV/WavPack) is re-encoded to a normalised 16-bit FLAC (capped at 48 kHz - the source's own bit depth and sample rate with `-k` - the embedded cover scaled to at most fullHD, unscaled with `-k covers`), keeping its source's modification time; everything else is copied across with `rsync` |
| Tidy | large cover images become AVIF (copied as they are with `-k covers`), stray video files are remuxed to MKV, and cue-sheet chapters are embedded into the flacs they describe. Cue sheets are read for their chapters first, and only the ones still without a FLAC of their own are then dropped |
| Tag | the assembled output is tagged and organised with `beets`, using the config that ships with the package (another with `-c` or the `beetsConfig` setting), with the import log written to `logs/beets.log`. Any albums skipped in this run are read back from `beets.log` and reported; when a web-searching language model is configured (`musicSearchLlm`, [models](../models.md)), it looks up each unmatched album's MusicBrainz release ID from the folder, track durations, existing tags and rip logs, and beets retries that folder quietly with `--search-id`; skipped with `-o` |
| Clean | passed through [`clean-folder-structure`](clean-folder-structure.md) for final name cleanup and empty-folder pruning |
| Copy | 120 kbps Opus copies are made with [`convert-audio`](convert-audio.md) |

**Re-runs add only what is new.** A run ends by cleaning the library's names, so
nothing in the library is called what this command called it — and a re-run must
therefore not make all of it a second time.

**Every phase counts what it is doing**, including the skips, so a re-run over a
library that is already ingested says so per track instead of looking like an
encoder that has stopped.

## Keeping everything (`-k`)

**A run with `-k` takes nothing away.** The `-k` is given in parts -
`resolution`, `downloads` and `covers`, comma-separated - and a bare `-k`
keeps all three.

`resolution` keeps the source's bit depth and sample rate: the track is still
re-encoded to FLAC - that is what makes the formats uniform - but at the
source's resolution rather than 16-bit at 48 kHz or less, and the Opus copies
made from the finished library afterwards are not affected.

`downloads` keeps the download folder as it was. A run would otherwise
de-duplicate it and split its multi-disc rips into subfolders in place; with
the part, the run works on a copy of the tree in its scratch instead, so the
de-duplication, the split and the renames happen on the copy and the folder
comes out of the run as it went in. When the copy cannot be made, the folders
the split would take apart are refused rather than ingested flat: each is
named with the reason, counted in the closing report, and left in the download
- not copied, not split, nothing changed - and the run goes on with the rest.

`covers` keeps the covers as they are. A run would otherwise turn a large
cover image into AVIF and scale the cover a track embeds down to fullHD; with
the part, the image is copied across as it is whatever its size, and the
embedded cover is written at its own size - still re-encoded as MJPEG, which
is what makes the art readable in a flac.

---

**Options:** `ingest-music -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses) ·
[what leaves the machine](../file-safety.md#what-leaves-the-machine) ·
[what a run prints](../output.md) ·
[settings](../settings.md)
