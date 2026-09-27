# `ingest-music`

Ingests a folder of freshly downloaded music into a clean, lossless FLAC
library, plus Opus copies to carry around.

    ingest-music [options] <downloadDir> <ingestDir> [opusCopyDir]

| At a glance | |
| --- | --- |
| **Takes** | a folder of downloaded music |
| **Writes** | the FLAC library into `<ingestDir>`, and 120 kbps Opus copies into `<opusCopyDir>` (default: the sibling folder `<ingestDir>opus`) |
| **Input** | never renamed: everything is applied to the output only |
| **Reruns** | add only what is new: running the same download folder again leaves the library exactly as it was |
| **Network** | yes — beets asks AcoustID, MusicBrainz, Last.fm, a lyrics site and a cover-art host ([details](../file-safety.md#what-leaves-the-machine)) |

## What it does

| Phase | What happens |
| --- | --- |
| Split | multi-disc CUE+image rips sharing one folder are split into a subfolder each (de-duplicating with `fdupes` first) |
| Encode | every lossless source (FLAC/APE/ALAC/WAV/WavPack) is re-encoded to a normalised 16-bit FLAC (capped at 48 kHz, embedded cover scaled to at most fullHD), keeping its source's modification time; everything else is copied across with `rsync` |
| Tidy | large cover images become AVIF, stray video files are remuxed to MKV, and cue-sheet chapters are embedded into the flacs they describe. Cue sheets are read for their chapters first, and only the ones still without a FLAC of their own are then dropped |
| Tag | the assembled output is tagged and organised with `beets`, using the config that ships with the package, with the import log written to `logs/beets.log` |
| Clean | passed through [`clean-folder-structure`](clean-folder-structure.md) for final name cleanup and empty-folder pruning |
| Copy | 120 kbps Opus copies are made with [`convert-audio`](convert-audio.md) |

**Re-runs add only what is new.** A run ends by cleaning the library's names, so
nothing in the library is called what this command called it — and a re-run must
therefore not make all of it a second time.

**Every phase counts what it is doing**, including the skips, so a re-run over a
library that is already ingested says so per track instead of looking like an
encoder that has stopped.

---

**Options:** `ingest-music -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses) ·
[what leaves the machine](../file-safety.md#what-leaves-the-machine)
