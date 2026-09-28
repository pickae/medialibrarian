# `convert-and-concat`

Converts ([`convert-audio`](convert-audio.md)) and then concatenates
([`concat-audio`](concat-audio.md)) in one run, keeping the intermediate tree
entirely in RAM.

    convert-and-concat [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | a folder with one subfolder — or archive — per book (per subfolder of those, with `-s`) |
| **Writes** | one finished book per subfolder into `<outputDir>`; nothing else reaches the disk |
| **Input** | file extensions lower-cased by the conversion, and folders cleaned with `-i`; archives are never touched |
| **Network** | none |

## Default behavior

With no options, a run:

- converts every book's audio to **Opus at 46 kbps**, keeping the source's channels, and copies the files too small to re-encode (`-e`, `-b`, `-m`; see [`convert-audio`](convert-audio.md#default-behavior))
- then joins one book per subfolder of the input (`-s` for one level deeper; `-c` skips the conversion)
- unpacks archives into RAM ([below](#archives))
- does not clean the input folders (`-i`), but the conversion does **lower-case the input's file extensions**
- converts everything again on a rerun, and fails on a book already in the output

Only the finished books are ever written to disk — which is why the output
folder is given rather than derived: it usually lives on a different disk than
the source.

| Codec (`-e`) | Intermediate tree | Finished book |
| --- | --- | --- |
| `opus` (default) | Opus | Opus |
| `xheaac` | xHE-AAC as `.m4a` ([needs exhale](convert-audio.md#setting-up-xhe-aac)) | `.m4b` |

## Archives

A book may arrive as an **archive instead of a folder**. Wherever the run expects a
folder of tracks — the input's subfolders, or theirs with `-s` — a `.zip`, `.rar`,
`.7z`, `.tar` or compressed tar lying there counts as one: it is unpacked into RAM
and ingested from there like a folder. The archive itself is never touched, and
only the extractors the input actually needs are required.

| Case | What happens |
| --- | --- |
| the archive holds **one folder** at its root | the unpacked folder is named for it — `dl-4471.zip` holding `Some Book/…` becomes `Some Book`, so a download renamed on its way here still carries the name it was packed under |
| the root holds anything else (several folders, files, or files beside a folder) | named for the archive file minus its suffix |
| a **folder of that name** lies beside the archive | the archive is left alone: that folder is taken to be it, already unpacked, and possibly corrected since |
| two archives claim one name (a `.zip` and a `.rar` of one book) | the first stands in for it and the other is passed over, so the two are never mixed into a single book |
| the redundant folder most archives carry (`Some Book.zip` holding `Some Book/…`) | dropped, so the tracks end up where they would have been unpacked by hand |
| a member asks to write **outside its own folder** — spelled `/etc/…` or `../…`, or a link pointing out of the tree | the archive is skipped whole rather than unpacked and tidied up afterwards. The same applies to `.cbz`/`.cbr`/`.cb7` in `convert-comics` |

An archive is read before it is unpacked, so that last check comes first.

---

**Options:** `convert-and-concat -h` lists every option and its default.
**Shared rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses)
