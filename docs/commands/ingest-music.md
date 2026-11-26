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
| **Network** | yes — beets asks AcoustID, MusicBrainz, Last.fm, a lyrics site and a cover-art host, and any unmatched album may be looked up on MusicBrainz through a web-searching language model under `-A` (`musicSearchLlm`), unless `-o` skips the tagging phase ([details](../file-safety.md#what-leaves-the-machine)) |

## Default behavior

With no options, a run:

- **de-duplicates the download folder and splits multi-disc rips there**, deleting identical files (on a copy of the tree with `-k downloads`, so the folder itself comes out of the run as it went in)
- re-encodes lossless tracks to 16-bit FLAC at most 48 kHz (the source's own resolution with `-k`), and copies the rest
- **tags and fetches covers, lyrics and genres with beets** (online; the phase is skipped with `-o`, and the config can be replaced with `-c`)
- cleans the names with [`clean-folder-structure`](clean-folder-structure.md), keeping the download's folders (another [layout](#library-layout) lets beets name them)
- makes 120 kbps Opus copies into `<ingestDir>opus` (`[opusCopyDir]`)
- adds only what is new on a rerun

Each phase is in the [table below](#what-it-does).

## What it does

| Phase | What happens |
| --- | --- |
| Split | multi-disc CUE+image rips sharing one folder are split into a subfolder each (de-duplicating with `fdupes` first). With `-k downloads` the de-duplication and the split happen on a copy of the tree, and a copy that cannot be made leaves the folders the split would take apart refused - not ingested flat, and not changed |
| Encode | every lossless source (FLAC/APE/ALAC/WAV/WavPack) is re-encoded to a normalised 16-bit FLAC (capped at 48 kHz - the source's own bit depth and sample rate with `-k` - the embedded cover scaled to at most fullHD, unscaled with `-k covers`), keeping its source's modification time; everything else is copied across with `rsync` |
| Tidy | large cover images become AVIF (copied as they are with `-k covers`), stray video files are remuxed to MKV, and cue-sheet chapters are embedded into the flacs they describe. Cue sheets are read for their chapters first, and only the ones still without a FLAC of their own are then dropped |
| Tag | the assembled output is tagged and organised with `beets`, using the config the package ships for the [layout](#library-layout) (another with `-c` or the `beetsConfig` setting), with the import log written to `logs/beets.log`. Any albums skipped in this run are read back from `beets.log` and reported; when the run is given `-A` and a web-searching language model is configured (`musicSearchLlm`, [models](../models.md)), it looks up each unmatched album's MusicBrainz release ID from the folder, track durations, existing tags and rip logs, and beets retries that folder quietly with `--search-id`. Then beets' [reports](#beets-reports) are read; skipped with `-o` |
| Clean | passed through [`clean-folder-structure`](clean-folder-structure.md) for final name cleanup and empty-folder pruning. In a layout where beets names the library, the names are left as beets gave them instead: the files beets left behind (rip logs, scans, other images) follow their album to its new folder, and the folders it emptied are removed |
| Copy | 120 kbps Opus copies are made with [`convert-audio`](convert-audio.md) |

**Re-runs add only what is new.** A run ends by cleaning the library's names, so
nothing in the library is called what this command called it — and a re-run must
therefore not make all of it a second time.

**Every phase counts what it is doing**, including the skips, so a re-run over a
library that is already ingested says so per track instead of looking like an
encoder that has stopped.

## Library layout

The `musicLayout` [setting](../settings.md) picks who names the library. Each
layout runs with a beets config that ships with the package, and `-c` or
`beetsConfig` still replaces it.

| `musicLayout` | Album | Track | Multi-disc | Compilation |
| --- | --- | --- | --- | --- |
| `keep` (default) | the download's folder, names cleaned | the download's name, cleaned | as downloaded | as downloaded |
| `plex` | `Artist/Album` | `01 - Title` | one folder, `101 - Title` (disc 1, track 1) | `Various Artists/Album` |
| `jellyfin` | `Artist/Album` | `01 - Title` | `Disc 1/01 - Title` | `Various Artists/Album` |
| `beets` | `Artist/Album` | `01 Title` | one folder, numbered across the discs | `Compilations/Album` |

Plex and Jellyfin both read the embedded tags first, so the names matter less
than the rule both servers keep: one album per folder. All the layouts except
`keep` follow that rule. In them, beets moves each album it matches. An
album it cannot match stays where the download put it, under the download's
name. The `plex` and `jellyfin` layouts also move a featured artist from the
artist tag into the title (`Song (feat. Guest)`), so the guest does not show
up as an artist of their own.

Every run re-imports the whole library, so switching an existing library to a
layout that moves albums moves every album beets can match, not just the new
ones. Only the albums in that run's download folder take their rip logs and
scans along. For older albums, those files stay in the old folder.

## Beets reports

After the import, the run asks beets three questions and logs the answers,
with a count of each in the closing stats. None of them changes a file.

| Report | Looks at | Finds |
| --- | --- | --- |
| missing tracks | the albums this run added | albums that lack tracks MusicBrainz lists |
| duplicates | the whole library | albums held twice, by MusicBrainz release |
| damaged files | the tracks this run added | files that fail their format's own check; needs the `flac` tool, and is skipped without it |

Albums with poor tags are also matched on their file names. With the
`discogsToken` [setting](../settings.md), beets matches against Discogs as
well as MusicBrainz. That needs beets' discogs plugin installed
(`python3-discogs-client`). It applies only to the shipped configs, since a
config named with `-c` or `beetsConfig` decides that for itself.

To refresh the tags of the whole library from MusicBrainz later, run beets
with the layout's shipped config, e.g. for the `plex` layout:

    beet -c <medialib>/config/beets-plex.yaml -d <ingestDir> mbsync

This works for the layouts where beets names the library. In the `keep`
layout, the names are cleaned after the import, so beets no longer knows
where the files are.

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
